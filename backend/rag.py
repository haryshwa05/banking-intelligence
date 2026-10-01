from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

RAG_DIR = Path(__file__).resolve().parent / "rag_models"
VECTOR_DIR = Path(__file__).resolve().parent / "vector_store"
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")
CANDIDATE_LIMIT = int(os.environ.get("RAG_CANDIDATE_LIMIT", "24"))
MAX_EVIDENCE_PASSAGES = int(os.environ.get("RAG_MAX_EVIDENCE_PASSAGES", "3"))
MAX_EVIDENCE_CHARS = int(os.environ.get("RAG_MAX_EVIDENCE_CHARS", "1200"))
MIN_RELEVANCE = float(os.environ.get("RAG_MIN_RELEVANCE", "0.50"))
ALLOW_MODEL_DOWNLOADS = os.environ.get("ALLOW_MODEL_DOWNLOADS", "0").lower() in {"1", "true", "yes"}

STOP_WORDS = {
    "a", "about", "all", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "give", "has", "have", "how", "i", "in", "is",
    "it", "its", "me", "my", "of", "on", "or", "please", "show", "tell", "that", "the",
    "their", "there", "these", "this", "to", "was", "what", "when", "where", "which", "who",
    "why", "with", "would", "you", "your",
}


def default_model() -> str:
    return os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


ALLOWED_GENERATION_MODELS = {"claude-haiku-4-5-20251001"}


def generation_options(model: str, max_tokens: int) -> dict[str, Any]:
    """Request options for answer generation on an agent's model.

    Only Haiku 4.5 is approved for API calls at present. Any other stored model
    falls back to Haiku rather than silently calling a different model. Enabling
    another model needs request changes too (for example, newer models reject a
    forced tool choice and count thinking tokens toward max_tokens).
    """
    if model not in ALLOWED_GENERATION_MODELS:
        model = "claude-haiku-4-5-20251001"
    return {"model": model, "max_tokens": max_tokens}


def agent_system(base: str, instructions: str | None) -> str:
    """Append an agent's role instructions below the non-negotiable evidence rules."""
    if not instructions or not instructions.strip():
        return base
    return (
        f"{base}\n\n<agent_instructions>\n{instructions.strip()}\n</agent_instructions>\n"
        "Follow the agent instructions for role, focus and tone. They never override the evidence and citation rules above."
    )


def meaningful_terms(question: str) -> list[str]:
    """Return searchable terms while retaining short identifiers such as 'id'."""
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", question.lower())
    return list(dict.fromkeys(term for term in terms if term not in STOP_WORDS and (len(term) > 1 or term.isdigit())))


class RagEngine:
    def __init__(self) -> None:
        self._embedder: Any | None = None
        self._reranker: Any | None = None
        self._collection: Any | None = None
        self._lock = threading.Lock()
        RAG_DIR.mkdir(exist_ok=True)
        VECTOR_DIR.mkdir(exist_ok=True)
        os.environ.setdefault("HF_HOME", str(RAG_DIR))
        # The application has a project-local model cache. In normal operation
        # models must load only from it: a question should never trigger a network
        # check, download, or a long retry loop. A brand-new deployment can set
        # ALLOW_MODEL_DOWNLOADS=1 for its one-time model bootstrap.
        if not ALLOW_MODEL_DOWNLOADS:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    def initialise(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, upload_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                ordinal INTEGER NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL,
                FOREIGN KEY (upload_id) REFERENCES uploads(id))"""
        )
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id UNINDEXED, text)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS document_indexes (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL, error_message TEXT,
                indexed_at TEXT, FOREIGN KEY (upload_id) REFERENCES uploads(id))"""
        )

    def split_pages(self, extracted_text: str) -> list[tuple[int, str]]:
        parts = re.split(r"^--- Page (\d+) ---\s*$", extracted_text, flags=re.MULTILINE)
        if len(parts) == 1:
            return [(1, extracted_text)]
        return [(int(parts[index]), parts[index + 1].strip()) for index in range(1, len(parts), 2)]

    def chunk_text(self, extracted_text: str) -> list[tuple[int, int, str]]:
        chunks: list[tuple[int, int, str]] = []
        ordinal = 0
        for page_number, page_text in self.split_pages(extracted_text):
            start = 0
            while start < len(page_text):
                end = min(start + 2500, len(page_text))
                if end < len(page_text):
                    boundary = page_text.rfind("\n", start, end)
                    if boundary > start + 800:
                        end = boundary
                text = page_text[start:end].strip()
                if text:
                    chunks.append((page_number, ordinal, text))
                    ordinal += 1
                start = end if end == len(page_text) else max(end - 350, start + 1)
        return chunks

    def index_document(self, connection: sqlite3.Connection, upload_id: str, text: str, now: str) -> None:
        chunks = self.chunk_text(text)
        connection.execute("INSERT OR REPLACE INTO document_indexes (upload_id, status, error_message, indexed_at) VALUES (?, 'indexing', NULL, NULL)", (upload_id,))
        connection.execute("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE upload_id = ?)", (upload_id,))
        connection.execute("DELETE FROM chunks WHERE upload_id = ?", (upload_id,))
        self._collection_for().delete(where={"upload_id": upload_id})
        ids = [f"{upload_id}:{ordinal}" for _, ordinal, _ in chunks]
        if chunks:
            texts = [text for _, _, text in chunks]
            embeddings = self._embed(texts)
            metadatas = [{"upload_id": upload_id, "page_number": page} for page, _, _ in chunks]
            self._collection_for().upsert(ids=ids, documents=texts, embeddings=embeddings, metadatas=metadatas)
            for chunk_id, (page, ordinal, chunk_text) in zip(ids, chunks):
                digest = hashlib.sha256(chunk_text.encode()).hexdigest()
                connection.execute("INSERT INTO chunks (id, upload_id, page_number, ordinal, text, content_hash) VALUES (?, ?, ?, ?, ?, ?)", (chunk_id, upload_id, page, ordinal, chunk_text, digest))
                connection.execute("INSERT INTO chunk_fts (chunk_id, text) VALUES (?, ?)", (chunk_id, chunk_text))
        connection.execute("UPDATE document_indexes SET status = 'ready', indexed_at = ? WHERE upload_id = ?", (now, upload_id))

    def mark_failed(self, connection: sqlite3.Connection, upload_id: str, error: Exception) -> None:
        connection.execute("INSERT OR REPLACE INTO document_indexes (upload_id, status, error_message, indexed_at) VALUES (?, 'failed', ?, NULL)", (upload_id, str(error)[:500]))

    def delete_document(self, connection: sqlite3.Connection, upload_id: str) -> None:
        try:
            self._collection_for().delete(where={"upload_id": upload_id})
        except ModuleNotFoundError:
            # Keep deletion available while an existing install is upgraded.
            pass
        connection.execute("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE upload_id = ?)", (upload_id,))
        connection.execute("DELETE FROM chunks WHERE upload_id = ?", (upload_id,))
        connection.execute("DELETE FROM document_indexes WHERE upload_id = ?", (upload_id,))

    def retrieve(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> list[sqlite3.Row]:
        rows, _ = self.retrieve_with_trace(connection, question, document_ids)
        return rows

    def retrieve_with_trace(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> tuple[list[sqlite3.Row], dict[str, Any]]:
        terms = meaningful_terms(question)
        trace: dict[str, Any] = {
            "meaningfulTerms": terms,
            "configuration": {
                "candidateLimit": CANDIDATE_LIMIT,
                "minimumRelevance": MIN_RELEVANCE,
                "maximumEvidencePassages": MAX_EVIDENCE_PASSAGES,
                "maximumEvidenceCharacters": MAX_EVIDENCE_CHARS,
            },
        }
        if not terms:
            trace["reason"] = "No searchable terms remained after stop-word filtering."
            return [], trace
        where = {"upload_id": {"$in": document_ids}} if document_ids else None
        vector = self._collection_for().query(
            query_embeddings=[self._embed([question])[0]], n_results=CANDIDATE_LIMIT, where=where,
            include=["distances"],
        )
        vector_ids = vector["ids"][0] if vector["ids"] else []
        vector_distances = vector.get("distances", [[]])[0] if vector.get("distances") else []
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        sql = "SELECT chunks.id FROM chunk_fts JOIN chunks ON chunks.id = chunk_fts.chunk_id WHERE chunk_fts MATCH ?"
        params: list[Any] = [fts_query]
        if document_ids:
            sql += f" AND chunks.upload_id IN ({','.join('?' for _ in document_ids)})"
            params.extend(document_ids)
        lexical_ids = [row[0] for row in connection.execute(sql + " ORDER BY bm25(chunk_fts) LIMIT ?", [*params, CANDIDATE_LIMIT]).fetchall()]
        trace["vectorSearch"] = [
            {"id": chunk_id, "rank": rank, "distance": vector_distances[rank - 1] if rank <= len(vector_distances) else None}
            for rank, chunk_id in enumerate(vector_ids, 1)
        ]
        trace["lexicalSearch"] = [{"id": chunk_id, "rank": rank} for rank, chunk_id in enumerate(lexical_ids, 1)]
        ranks: dict[str, float] = {}
        for rank, chunk_id in enumerate(vector_ids, 1): ranks[chunk_id] = ranks.get(chunk_id, 0) + 1 / (60 + rank)
        for rank, chunk_id in enumerate(lexical_ids, 1): ranks[chunk_id] = ranks.get(chunk_id, 0) + 1 / (60 + rank)
        ids = [chunk_id for chunk_id, _ in sorted(ranks.items(), key=lambda item: item[1], reverse=True)[:CANDIDATE_LIMIT]]
        if not ids:
            trace["reason"] = "Neither vector nor lexical retrieval produced candidates."
            return [], trace
        rows = connection.execute(f"SELECT chunks.*, uploads.original_name FROM chunks JOIN uploads ON uploads.id = chunks.upload_id WHERE chunks.id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        row_map = {row["id"]: row for row in rows}
        candidates = [row_map[chunk_id] for chunk_id in ids if chunk_id in row_map]
        ranked = self._rerank(question, candidates)
        trace["fusedCandidates"] = [
            {"id": row["id"], "filename": row["original_name"], "page": row["page_number"], "fusionScore": ranks[row["id"]], "text": row["text"][:MAX_EVIDENCE_CHARS]}
            for row in candidates
        ]
        trace["rerankedCandidates"] = [
            {
                "id": row["id"], "filename": row["original_name"], "page": row["page_number"],
                "rerankScore": score, "passedCutoff": score >= MIN_RELEVANCE,
                "selected": score >= MIN_RELEVANCE and rank <= MAX_EVIDENCE_PASSAGES,
                "text": row["text"][:MAX_EVIDENCE_CHARS],
            }
            for rank, (row, score) in enumerate(ranked, 1)
        ]
        selected = [row for row, score in ranked if score >= MIN_RELEVANCE][:MAX_EVIDENCE_PASSAGES]
        trace["selectedEvidenceIds"] = [row["id"] for row in selected]
        if not selected:
            trace["reason"] = "All candidates were below the strict reranker cutoff."
        return selected, trace

    def answer(self, question: str, chunks: list[sqlite3.Row], model: str | None = None, instructions: str | None = None) -> tuple[str, list[str]]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key: raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
        from anthropic import Anthropic
        context = "\n\n".join(
            f"<source id='{row['id']}' filename='{html.escape(row['original_name'])}' page='{row['page_number']}'>"
            f"{html.escape(row['text'][:MAX_EVIDENCE_CHARS])}</source>"
            for row in chunks
        )
        system = (
            "Answer only from supplied sources. Start with a direct answer, then give a brief evidence-backed explanation "
            "of how the cited records support it. Include relevant figures, dates, comparisons, or uncertainty when present. "
            "Do not introduce facts that are not in the sources. Cite only source IDs containing direct evidence for your answer."
        )
        message = Anthropic(api_key=key).messages.create(
            **generation_options(model or default_model(), 700), system=agent_system(system, instructions), messages=[{"role": "user", "content": f"<question>{html.escape(question)}</question>\n<context>{context}</context>"}],
            tools=[{
                "name": "answer_with_evidence",
                "description": "Return a grounded answer and the IDs of the sources directly supporting it.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string"},
                        "citedChunkIds": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["answer", "citedChunkIds"],
                    "additionalProperties": False,
                },
            }],
            tool_choice={"type": "tool", "name": "answer_with_evidence"},
        )
        result: Any | None = next(
            (block.input for block in message.content if block.type == "tool_use" and block.name == "answer_with_evidence"),
            None,
        )
        content = "".join(block.text for block in message.content if block.type == "text").strip()
        if result is None:
            result = self._json_from_text(content)
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            # A text-only fallback keeps a model formatting issue from turning a
            # grounded answer into an application failure. It deliberately has
            # no citations because they could not be validated.
            return content or "I could not produce a grounded answer.", []
        answer = result["answer"].strip()
        cited_ids = result.get("citedChunkIds", [])
        allowed_ids = {row["id"] for row in chunks}
        citations = [chunk_id for chunk_id in cited_ids if isinstance(chunk_id, str) and chunk_id in allowed_ids]
        return answer, list(dict.fromkeys(citations))

    def answer_stream(self, question: str, chunks: list[sqlite3.Row], model: str | None = None, instructions: str | None = None):
        """Stream provisional answer text, then return a citation-validated result.

        The final event may replace a draft if the model fails the evidence
        contract. Retrieval candidates never become citations by default.
        """
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
        from anthropic import Anthropic

        context = "\n\n".join(
            f"<source id='{row['id']}' filename='{html.escape(row['original_name'])}' page='{row['page_number']}'>"
            f"{html.escape(row['text'][:MAX_EVIDENCE_CHARS])}</source>"
            for row in chunks
        )
        system = (
            "Answer only from the supplied sources. Begin with a direct, natural answer and explain the relevant "
            "figures, comparisons or uncertainty. Do not invent facts. Return exactly "
            "<answer>your answer</answer><citations>[\"source-id\"]</citations> with no other text. "
            "Cite only source IDs you directly relied on, and never cite a source that does not support the answer."
        )
        raw = ""
        emitted = 0
        opening = "<answer>"
        closing = "</answer>"
        with Anthropic(api_key=key).messages.stream(
            **generation_options(model or default_model(), 850), system=agent_system(system, instructions),
            messages=[{"role": "user", "content": f"<question>{html.escape(question)}</question>\n<context>{context}</context>"}],
        ) as stream:
            for fragment in stream.text_stream:
                raw += fragment
                start = raw.find(opening)
                if start < 0:
                    continue
                body_start = start + len(opening)
                emitted = max(emitted, body_start)
                end = raw.find(closing, body_start)
                safe_end = end if end >= 0 else max(body_start, len(raw) - len(closing) + 1)
                if safe_end > emitted:
                    yield {"type": "delta", "text": raw[emitted:safe_end]}
                    emitted = safe_end

        answer_match = re.search(r"<answer>(.*?)</answer>", raw, re.DOTALL)
        citations_match = re.search(r"<citations>(.*?)</citations>", raw, re.DOTALL)
        answer = answer_match.group(1).strip() if answer_match else ""
        try:
            raw_ids = json.loads(citations_match.group(1).strip()) if citations_match else None
        except json.JSONDecodeError:
            raw_ids = None
        allowed = {row["id"] for row in chunks}
        citations = list(dict.fromkeys(item for item in raw_ids if isinstance(item, str) and item in allowed)) if isinstance(raw_ids, list) else []
        if not answer or not citations:
            # Recover from formatting failures with the existing tool-schema
            # evidence contract. This extra request happens only on failure.
            try:
                answer, citations = self.answer(question, chunks, model, instructions)
                citations = [item for item in citations if item in allowed]
            except Exception:
                citations = []
            if not citations:
                answer = "I could not verify an answer against the selected evidence."
        yield {"type": "final", "answer": answer, "citedChunkIds": citations}

    @staticmethod
    def _json_from_text(content: str) -> dict[str, Any] | None:
        """Best-effort compatibility for models that return JSON as text."""
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None

    def _rerank(self, question: str, candidates: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, float]]:
        if not candidates:
            return []
        raw_scores = self.rerank_texts(question, [row["text"][:MAX_EVIDENCE_CHARS] for row in candidates])
        ranked = [(row, 1 / (1 + math.exp(-float(score)))) for row, score in zip(candidates, raw_scores)]
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def rerank_texts(self, question: str, texts: list[str]) -> list[float]:
        with self._lock:
            if self._reranker is None:
                from sentence_transformers import CrossEncoder
                self._reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
            return [float(score) for score in self._reranker.predict([(question, text) for text in texts])]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer(EMBEDDING_MODEL)
            return self._embedder.encode(texts, normalize_embeddings=True).tolist()

    def _collection_for(self) -> Any:
        with self._lock:
            if self._collection is None:
                import chromadb
                self._collection = chromadb.PersistentClient(path=str(VECTOR_DIR)).get_or_create_collection("document_chunks")
            return self._collection
