# Document Intelligence

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

Open `http://localhost:4200/` and sign in with username `admin` and password `admin`. This is a frontend-only prototype entry screen, not backend authentication. Every fresh page load starts at sign-in; the account circle in the top-right corner offers **Log out**, which returns to sign-in without deleting stored chats or documents.

The sidebar separates Chat from the Document Repository. The repository retains upload, preview, extraction, ownership review, and deletion. In a new chat, choose automatic scope (the full library, narrowed when a customer name resolves), one customer, or selected documents. The scope is fixed for that conversation; start a new chat to change it. The API enforces it for both structured facts and RAG retrieval.

Chats and messages are stored in the same local SQLite database as document metadata. Answers stream over `POST /conversations/{id}/messages/stream`: progress appears during context resolution and retrieval, RAG answer text streams as Claude generates it, and the final event supplies validated citations and developer trace. Structured calculations are verified before their answer text is delivered. Stop interrupts the current attempt, which can be retried without duplicating the user turn. A follow-up uses recent turns and a bounded summary to form a standalone retrieval question, but old chat answers are never treated as document evidence. Chats can be renamed or deleted from the sidebar.

This is still a single-user local prototype with no login or per-user isolation. Add authentication and authorization before using it as a shared service for real customer data.

Customer assignments are explicit reviewer choices. They update document ownership without treating every identifier in that document as a verified identifier for the chosen customer. Reassigning a document removes identifier evidence previously attributed to its former customer.

## Knowledge Spaces

Every document belongs to exactly one knowledge context, shown in **Knowledge Spaces** and in the repository's *Knowledge context* column:

- **Entity knowledge**: customer-specific documents (applications, salary slips, statements, ID, address proof). A document is entity knowledge unless it is filed into a shared space, and its owner is the customer resolved automatically or assigned by a reviewer.
- **Reference knowledge**: shared policies, regulations, product rules and eligibility criteria (seeded: *Lending Policies*, *Compliance & Regulatory*).
- **Operational knowledge**: shared SOPs, checklists and escalation guides (seeded: *Loan Review Procedures*).

Choose a shared space in the upload bar's **File into** selector, or move an existing document from its detail panel. Filing a document into a shared space removes any customer ownership and the identifiers extracted from it, and automatic customer resolution never runs on shared documents. Moving it back to customer knowledge re-runs customer detection.

## Agents

Chats are held with specialised agents. Each agent has a name, purpose, description, instructions, model, knowledge access and capabilities, all editable in **Agent Builder**. Three agents are built in: Customer Document Analyst (customer documents only), Loan Eligibility Analyst (customer + Lending Policies) and Compliance Analyst (Compliance & Regulatory, plus a customer if one is chosen).

- **Reusable, customer-bound chats**: an agent never stores a customer. Each chat binds the agent to one customer, chosen when the chat starts and fixed from then on. The same agent therefore works for Tony Stark in one chat and Bruce Wayne in another, with separate histories.
- **Governed retrieval**: for every turn the API resolves the agent's effective knowledge (the chat customer's own documents plus the agent's granted spaces) and passes only those document IDs to fact lookup and retrieval. A question naming a different customer is refused, and an empty scope is never widened to the full library. Changing an agent's access applies to its existing chats from their next question.
- **Capabilities are enforced**, not just described: passage search, fact lookup, verified calculations, policy-rule evaluation, customer overview and a deterministic cross-document consistency check each gate an engine route.
- **Visible context**: every agent chat shows the agent, the customer and the knowledge in use, with document counts. Each answer records the knowledge it was produced from.
- **Models**: only Claude Haiku 4.5 is approved for API calls. The builder offers only Haiku, and the API rejects any other model.

Chats created before agents existed are still available under **Earlier chats**.

## Developer trace

Completed answers have a collapsed **Show developer trace** control. It is intentionally for local development only and includes document text excerpts. It shows the searchable terms, vector and SQLite full-text candidates, fused ranks, reranker scores/cutoff decisions, selected structured facts, the calculation/selection plan, the RAG passages sent to Claude, and the final validated citation IDs.

## Verification

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest test_facts.py test_entity_resolution.py test_scope_api.py test_chat_api.py test_rag_stream.py test_agents_api.py

cd ..\frontend
npm run build
```

## Document understanding limits

- PDFs and images are extracted in English.
- Files must be 50 MB or smaller; PDFs may contain up to 100 pages.
- Office documents are stored and listed, but text extraction for them is not included yet.
- The first embedding or reranking run downloads the local models to `backend/rag_models/`; later indexing and reranking are local. Normal runtime is cache-only; set `ALLOW_MODEL_DOWNLOADS=1` temporarily in `backend/.env` only when bootstrapping models on a new machine.
- Claude access is optional but required to turn retrieved passages into natural-language answers. Its model is configured with `CLAUDE_MODEL` in `backend/.env`.
