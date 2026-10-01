# Document Library

Upload images, PDFs, and common office documents from the Angular app. Files are saved locally under `backend/uploads/`, while their names, MIME types, sizes, upload times, and extraction status are stored in a local SQLite database.

PDFs and images are understood locally. The service extracts embedded PDF text first and uses PaddleOCR only for scanned PDF pages and images. The first OCR job downloads its free English model into `backend/ocr_models/`; later extraction stays on this computer.

Completed PDFs and images are automatically chunked and indexed locally with BAAI `bge-base-en-v1.5`, Chroma, and SQLite full-text search. The app combines semantic and keyword retrieval, reranks candidates locally with BAAI `bge-reranker-base`, and sends only a small set of strict-evidence passages to Claude. Source chips represent only passages Claude explicitly cited. Chunk retrieval works for any extracted English text; the current structured concept ontology and calculation rules focus on financial documents.

Completed documents also receive a page-level, provenance-backed fact pass. Claude extracts labelled values from the already-local extracted text; Python records normalized numbers, currencies, types, and page evidence in SQLite and indexes the fact wording in Chroma. A later mapping pass assigns concepts from a controlled financial ontology, such as employer name and employer address. At question time the service can retrieve typed facts, and Python uses `Decimal` for supported calculations. Simple explicit multiplier policies such as `12 times gross monthly income` can be evaluated across documents, with a refusal when currencies differ. The structured-fact status and evidence are visible in the document panel.

## Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Add ANTHROPIC_API_KEY to backend/.env to enable document questions.
.\run-backend.ps1
```

The API is available at `http://localhost:8000/`. Interactive API documentation is at `http://localhost:8000/docs`.

`run-backend.ps1` is the normal command: it does not watch files, so the local
models load once and stay in memory. Use `./run-backend-dev.ps1` only while
editing backend source code. In development mode, every saved `.py` source file
causes one intentional restart; generated uploads, vector indexes, model caches,
database updates, and test files are excluded.

## Frontend

```powershell
cd frontend
npm install
npm start
```

Open `http://localhost:4200/` to upload and browse documents. Select a file to see its extraction progress and read completed text. In document details, assign a file that needs ownership review to an existing customer, or create a customer and assign it. Use **Ask your documents** after indexing finishes. Choose automatic scope (the full library, narrowed when a customer name resolves), one customer's linked documents, or one or more selected documents. A selected scope is enforced by the API for both structured facts and RAG retrieval.

Customer assignments are explicit reviewer choices. They update document ownership without treating every identifier in that document as a verified identifier for the chosen customer. Reassigning a document removes identifier evidence previously attributed to its former customer.

## Developer trace

Every question response currently includes an expanded **Developer trace** panel in the Angular UI. It is intentionally for local development only and includes document text excerpts. It shows the searchable terms, vector and SQLite full-text candidates, fused ranks, reranker scores/cutoff decisions, selected structured facts, the calculation/selection plan, the RAG passages sent to Claude, and the final validated citation IDs.

## Verification

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest test_facts.py test_entity_resolution.py test_scope_api.py

cd ..\frontend
npm run build
```

## Document understanding limits

- PDFs and images are extracted in English.
- Files must be 50 MB or smaller; PDFs may contain up to 100 pages.
- Office documents are stored and listed, but text extraction for them is not included yet.
- The first embedding or reranking run downloads the local models to `backend/rag_models/`; later indexing and reranking are local. Normal runtime is cache-only; set `ALLOW_MODEL_DOWNLOADS=1` temporarily in `backend/.env` only when bootstrapping models on a new machine.
- Claude access is optional but required to turn retrieved passages into natural-language answers. Its model is configured with `CLAUDE_MODEL` in `backend/.env`.
