from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from rag import meaningful_terms

FACT_VECTOR_DIR = Path(__file__).resolve().parent / "vector_store"
FACT_EXTRACTION_VERSION = "generic-facts-v1"
PROFILE_REQUEST_TERMS = {"about", "overview", "profile", "summary", "summarize", "background"}
PROFILE_GENERIC_TERMS = {"customer", "applicant", "client", "person", "profile", "overview", "summary", "summarize", "background", "details", "information", "tell", "give"}
PROFILE_FACTS_PER_DOCUMENT = 40
PROFILE_FACT_LIMIT = 120
LIMIT_REQUEST_TERMS = {
    "within", "eligible", "eligibility", "limit", "limited", "allowed", "allowable",
    "maximum", "minimum", "cap", "ceiling", "threshold", "exceed", "exceeds",
}
MULTIPLIER_PATTERN = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?\s*(?:times|x|\*)", re.I)
CONCEPT_VERSION = "financial-concepts-v1"
CANONICAL_CONCEPTS = [
    "person.full_name", "person.customer_id", "person.application_id", "person.government_id", "person.account_number", "person.email", "person.phone", "person.date_of_birth",
    "employment.employer.name", "employment.employer.address", "employment.employer.code", "employment.employee_id", "employment.job_title", "employment.type",
    "income.gross_monthly", "income.net_monthly", "income.annual_gross",
    "application.requested_amount", "application.loan_type", "application.loan_term", "application.purpose",
    "policy.maximum_amount", "policy.minimum_income", "policy.rule", "obligation.monthly_emi", "address.residential", "none",
]


class FactEngine:
    """Extracts provenance-backed facts and executes only generic numeric operations."""

    def __init__(self, embed_texts: Callable[[list[str]], list[list[float]]], rerank: Callable[[str, list[str]], list[float]]) -> None:
        self._embed_texts = embed_texts
        self._rerank = rerank
        self._collection: Any | None = None
        FACT_VECTOR_DIR.mkdir(exist_ok=True)

    def initialise(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS document_facts (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL, error_message TEXT,
                extracted_at TEXT, extraction_version TEXT NOT NULL,
                FOREIGN KEY (upload_id) REFERENCES uploads(id))"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                fact_group_id TEXT NOT NULL, raw_label TEXT NOT NULL, raw_value TEXT NOT NULL,
                normalized_value TEXT, value_type TEXT NOT NULL, currency TEXT, period TEXT,
                subject TEXT, canonical_concept TEXT, evidence_text TEXT NOT NULL, confidence REAL, content_hash TEXT NOT NULL,
                extraction_version TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES uploads(id))"""
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(facts)").fetchall()}
        if "canonical_concept" not in columns:
            connection.execute("ALTER TABLE facts ADD COLUMN canonical_concept TEXT")
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(fact_id UNINDEXED, text)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS document_concepts (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL, error_message TEXT,
                mapped_at TEXT, concept_version TEXT NOT NULL,
                FOREIGN KEY(upload_id) REFERENCES uploads(id))"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO document_concepts(upload_id, status, error_message, mapped_at, concept_version)
            SELECT document_id, 'queued', NULL, NULL, ? FROM facts GROUP BY document_id""",
            (CONCEPT_VERSION,),
        )

    def queue_document(self, connection: sqlite3.Connection, upload_id: str) -> None:
        connection.execute(
            """INSERT INTO document_facts (upload_id, status, extraction_version)
            VALUES (?, 'queued', ?)
            ON CONFLICT(upload_id) DO UPDATE SET status='queued', error_message=NULL, extracted_at=NULL,
            extraction_version=excluded.extraction_version""",
            (upload_id, FACT_EXTRACTION_VERSION),
        )

    def delete_document(self, connection: sqlite3.Connection, upload_id: str) -> None:
        try:
            self._collection_for().delete(where={"document_id": upload_id})
        except ModuleNotFoundError:
            pass
        connection.execute("DELETE FROM fact_fts WHERE fact_id IN (SELECT id FROM facts WHERE document_id = ?)", (upload_id,))
        connection.execute("DELETE FROM facts WHERE document_id = ?", (upload_id,))
        connection.execute("DELETE FROM document_facts WHERE upload_id = ?", (upload_id,))

    def extract_document(self, connection: sqlite3.Connection, upload_id: str, text: str, now: str) -> None:
        self.queue_document(connection, upload_id)
        connection.execute("UPDATE document_facts SET status='processing' WHERE upload_id=?", (upload_id,))
        connection.execute("DELETE FROM fact_fts WHERE fact_id IN (SELECT id FROM facts WHERE document_id = ?)", (upload_id,))
        connection.execute("DELETE FROM facts WHERE document_id = ?", (upload_id,))
        self._collection_for().delete(where={"document_id": upload_id})
        facts: list[dict[str, Any]] = []
        for page_number, page_text in self._pages(text):
            facts.extend(self._extract_page(page_number, page_text))
        ids: list[str] = []
        vector_texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for ordinal, fact in enumerate(facts):
            validated = self._validate_fact(fact)
            if validated is None:
                continue
            fact_id = f"{upload_id}:fact:{ordinal}"
            digest = hashlib.sha256(json.dumps(validated, sort_keys=True).encode()).hexdigest()
            connection.execute(
                """INSERT INTO facts (id, document_id, page_number, fact_group_id, raw_label, raw_value,
                normalized_value, value_type, currency, period, subject, canonical_concept, evidence_text,
                confidence, content_hash, extraction_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fact_id, upload_id, validated["pageNumber"], f"{upload_id}:{validated['pageNumber']}",
                 validated["rawLabel"], validated["rawValue"], validated["normalizedValue"], validated["valueType"],
                 validated["currency"], validated["period"], validated["subject"], validated["canonicalConcept"], validated["evidenceText"],
                 validated["confidence"], digest, FACT_EXTRACTION_VERSION),
            )
            searchable = " | ".join(filter(None, [validated["rawLabel"], validated["rawValue"], validated["subject"], validated["evidenceText"]]))
            connection.execute("INSERT INTO fact_fts (fact_id, text) VALUES (?, ?)", (fact_id, searchable))
            ids.append(fact_id)
            vector_texts.append(searchable)
            metadatas.append({"document_id": upload_id, "page_number": validated["pageNumber"]})
        if ids:
            self._collection_for().upsert(ids=ids, documents=vector_texts, embeddings=self._embed_texts(vector_texts), metadatas=metadatas)
        connection.execute("UPDATE document_facts SET status='ready', error_message=NULL, extracted_at=? WHERE upload_id=?", (now, upload_id))
        self.queue_concept_mapping(connection, upload_id)

    def queue_concept_mapping(self, connection: sqlite3.Connection, upload_id: str) -> None:
        connection.execute(
            """INSERT INTO document_concepts(upload_id, status, error_message, mapped_at, concept_version)
            VALUES (?, 'queued', NULL, NULL, ?)
            ON CONFLICT(upload_id) DO UPDATE SET status='queued', error_message=NULL, mapped_at=NULL, concept_version=excluded.concept_version""",
            (upload_id, CONCEPT_VERSION),
        )

    def map_document_concepts(self, connection: sqlite3.Connection, upload_id: str, now: str) -> None:
        connection.execute("UPDATE document_concepts SET status='processing', error_message=NULL WHERE upload_id=?", (upload_id,))
        rows = connection.execute("SELECT id, raw_label, raw_value, page_number FROM facts WHERE document_id=? ORDER BY page_number, id", (upload_id,)).fetchall()
        mappings = self._map_concepts(rows)
        for fact_id, concept in mappings.items():
            connection.execute("UPDATE facts SET canonical_concept=? WHERE id=? AND document_id=?", (concept, fact_id, upload_id))
        connection.execute("UPDATE document_concepts SET status='ready', error_message=NULL, mapped_at=? WHERE upload_id=?", (now, upload_id))

    def mark_concept_mapping_failed(self, connection: sqlite3.Connection, upload_id: str, error: Exception) -> None:
        connection.execute("UPDATE document_concepts SET status='failed', error_message=? WHERE upload_id=?", (str(error)[:500], upload_id))

    def mark_failed(self, connection: sqlite3.Connection, upload_id: str, error: Exception) -> None:
        connection.execute("INSERT OR REPLACE INTO document_facts VALUES (?, 'failed', ?, NULL, ?)", (upload_id, str(error)[:500], FACT_EXTRACTION_VERSION))

    def answer(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> tuple[str, list[sqlite3.Row]] | None:
        result, _ = self.answer_with_trace(connection, question, document_ids)
        return result

    def answer_with_trace(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> tuple[tuple[str, list[sqlite3.Row]] | None, dict[str, Any]]:
        trace: dict[str, Any] = {"route": "structured-facts"}
        if connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0:
            trace["reason"] = "No structured facts are available yet."
            return None, trace
        concept_plan = self._plan_question_concept(question)
        trace["conceptPlan"] = concept_plan
        concept = concept_plan.get("concept") if isinstance(concept_plan, dict) else "none"
        if isinstance(concept, str) and concept != "none":
            concept_rows = self._retrieve_concept(connection, concept, document_ids)
            trace["conceptCandidates"] = [self._trace_fact(row) for row in concept_rows]
            if concept_rows:
                scores = self._rerank(question, [self._fact_text(row) for row in concept_rows])
                row = max(zip(concept_rows, scores), key=lambda item: item[1])[0]
                answer = self._narrate_verified_fact(question, row)
                trace["outcome"] = "canonical concept lookup"
                trace["selectedFacts"] = [self._trace_fact(row)]
                trace["narration"] = {"mode": "llm-verified" if answer else "deterministic-fallback", "accepted": bool(answer)}
                return (
                    answer or f"{row['raw_label']}: {row['raw_value']}\n\nEvidence:\n{self._evidence_summary([row])}",
                    [row],
                ), trace
            trace["reason"] = f"No facts mapped to requested concept: {concept}."
            return None, trace
        fact_rows, retrieval_trace = self._retrieve_with_trace(connection, question, document_ids)
        trace["retrieval"] = retrieval_trace
        if not fact_rows:
            trace["reason"] = "No fact candidates passed the relevance cutoff."
            return None, trace
        profile_request = bool(retrieval_trace.get("profileRequest"))
        deterministic_plan = retrieval_trace.get("deterministicFormulaPlan")
        if isinstance(deterministic_plan, dict):
            by_id = {row["id"]: row for row in fact_rows}
            ordered_ids = [
                deterministic_plan.get("targetFactId"),
                deterministic_plan.get("formulaFactId"),
                deterministic_plan.get("baseFactId"),
            ]
            selected = [by_id[fact_id] for fact_id in ordered_ids if isinstance(fact_id, str) and fact_id in by_id]
            result = self._formula_compare(selected, deterministic_plan)
            trace["selectionPlan"] = deterministic_plan
            trace["selectedFacts"] = [self._trace_fact(row) for row in selected]
            if result:
                trace["outcome"] = "deterministic formula comparison"
                trace["calculationEvidence"] = [self._trace_fact(row) for row in result[1]]
                natural_answer = self._narrate_verified_formula(question, selected, deterministic_plan)
                trace["narration"] = {
                    "mode": "llm-verified" if natural_answer else "deterministic-fallback",
                    "accepted": bool(natural_answer),
                }
                return (natural_answer or result[0], result[1]), trace
            # Do not silently use a language-model plan if deterministic evidence
            # selection could not be executed safely.
            trace["reason"] = "The deterministic formula evidence could not be safely calculated."
            return (
                "I found an explicit policy formula and related values, but cannot safely calculate the result from "
                "the available normalized evidence. Please review the cited document values.",
                selected,
            ), trace
        plan = self._resolve(question, fact_rows, profile_request)
        trace["selectionPlan"] = plan
        # The resolver's order is meaningful for subtraction/comparisons.  Preserve it
        # instead of relying on SQLite/vector retrieval order.
        by_id = {row["id"]: row for row in fact_rows}
        selected = [by_id[fact_id] for fact_id in plan.get("factIds", []) if fact_id in by_id]
        trace["selectedFacts"] = [self._trace_fact(row) for row in selected]
        if not selected:
            trace["reason"] = "The fact selector did not choose valid retrieved fact IDs."
            return None, trace
        operation = plan.get("operation")
        if operation == "profile":
            profile = plan.get("answer")
            if isinstance(profile, str) and profile.strip():
                trace["outcome"] = "customer profile"
                return (profile.strip(), selected), trace
            trace["reason"] = "The profile planner did not return a grounded overview."
            return None, trace
        if operation == "fact_lookup":
            row = selected[0]
            trace["outcome"] = "fact lookup"
            natural_answer = self._narrate_verified_fact(question, row)
            trace["narration"] = {"mode": "llm-verified" if natural_answer else "deterministic-fallback", "accepted": bool(natural_answer)}
            return (
                natural_answer or f"{row['raw_label']}: {row['raw_value']}\n\nEvidence:\n{self._evidence_summary([row])}",
                [row],
            ), trace
        if operation in {"sum", "average", "minimum", "maximum", "count", "difference", "compare", "formula_compare"}:
            result = self._calculate(operation, selected, plan)
            trace["outcome"] = "calculation" if result else "calculation could not be safely executed"
            if result:
                trace["calculationEvidence"] = [self._trace_fact(row) for row in result[1]]
            return result, trace
        trace["reason"] = f"The fact selector returned unsupported operation: {operation!r}."
        return None, trace

    def _retrieve_concept(self, connection: sqlite3.Connection, concept: str, document_ids: list[str] | None) -> list[sqlite3.Row]:
        sql = "SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id WHERE facts.canonical_concept=?"
        params: list[Any] = [concept]
        if document_ids:
            sql += f" AND facts.document_id IN ({','.join('?' for _ in document_ids)})"
            params.extend(document_ids)
        return connection.execute(sql + " ORDER BY facts.page_number, facts.id LIMIT 40", params).fetchall()

    @staticmethod
    def _plan_question_concept(question: str) -> dict[str, Any]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return {"concept": "none", "reason": "ANTHROPIC_API_KEY is not configured."}
        try:
            from anthropic import Anthropic
            message = Anthropic(api_key=key).messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=180,
                system=(
                    "Map the question's requested answer to one canonical concept. Do not select a document or a fact. "
                    "For 'where does a person work', choose employment.employer.name; for an employer location/address, choose employment.employer.address. "
                    "Choose none for comparisons, calculations, summaries, multi-fact questions, or concepts not in the supplied list."
                ),
                messages=[{"role": "user", "content": question}],
                tools=[{
                    "name": "select_concept", "description": "Select the one requested canonical concept, if any.",
                    "input_schema": {"type": "object", "properties": {"concept": {"type": "string", "enum": CANONICAL_CONCEPTS}}, "required": ["concept"], "additionalProperties": False},
                }], tool_choice={"type": "tool", "name": "select_concept"},
            )
            result = next((block.input for block in message.content if block.type == "tool_use"), {"concept": "none"})
            return result if isinstance(result, dict) else {"concept": "none"}
        except Exception:
            return {"concept": "none", "reason": "Concept planner unavailable."}

    def _retrieve(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> list[sqlite3.Row]:
        rows, _ = self._retrieve_with_trace(connection, question, document_ids)
        return rows

    def _retrieve_with_trace(self, connection: sqlite3.Connection, question: str, document_ids: list[str] | None) -> tuple[list[sqlite3.Row], dict[str, Any]]:
        trace: dict[str, Any] = {"candidateLimit": 40, "minimumRelevance": 0.50, "maximumSelected": 20}
        profile_request = self._is_profile_request(question)
        trace["profileRequest"] = profile_request
        where = {"document_id": {"$in": document_ids}} if document_ids else None
        vector = self._collection_for().query(query_embeddings=[self._embed_texts([question])[0]], n_results=40, where=where)
        vector_ids = vector["ids"][0] if vector["ids"] else []
        terms = meaningful_terms(question)
        trace["terms"] = terms
        if not terms:
            trace["reason"] = "No searchable terms remained after filtering."
            return [], trace
        sql = "SELECT facts.id FROM fact_fts JOIN facts ON facts.id=fact_fts.fact_id WHERE fact_fts MATCH ?"
        params: list[Any] = [" OR ".join(f'"{term}"' for term in terms)]
        if document_ids:
            sql += f" AND facts.document_id IN ({','.join('?' for _ in document_ids)})"
            params.extend(document_ids)
        lexical_ids = [row[0] for row in connection.execute(sql + " ORDER BY bm25(fact_fts) LIMIT 40", params).fetchall()]
        trace["vectorSearch"] = [{"id": fact_id, "rank": rank} for rank, fact_id in enumerate(vector_ids, 1)]
        trace["lexicalSearch"] = [{"id": fact_id, "rank": rank} for rank, fact_id in enumerate(lexical_ids, 1)]
        ids = list(dict.fromkeys([*vector_ids, *lexical_ids]))
        if not ids:
            trace["reason"] = "Neither vector nor lexical retrieval produced candidates."
            return [], trace
        rows = connection.execute(f"SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id WHERE facts.id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        by_id = {row["id"]: row for row in rows}
        candidates = [by_id[item] for item in ids if item in by_id]
        dependency_ids, dependency_trace = self._formula_dependency_ids(connection, candidates, document_ids)
        missing_dependency_ids = [fact_id for fact_id in dependency_ids if fact_id not in by_id]
        if missing_dependency_ids:
            dependency_rows = connection.execute(
                f"SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id WHERE facts.id IN ({','.join('?' for _ in missing_dependency_ids)})",
                missing_dependency_ids,
            ).fetchall()
            dependency_map = {row["id"]: row for row in dependency_rows}
            candidates.extend(dependency_map[fact_id] for fact_id in missing_dependency_ids if fact_id in dependency_map)
        deterministic_plan = self._deterministic_formula_plan(question, candidates)
        profile_rows, profile_trace = self._profile_candidates(connection, question, candidates, document_ids) if profile_request else ([], {"reason": "Not a profile request."})
        if profile_rows:
            candidates = profile_rows
        raw_scores = self._rerank(question, [self._fact_text(row) for row in candidates])
        # CrossEncoder returns logits.  The configured 0.50 cutoff is a probability,
        # so applying it directly to raw logits rejects almost every useful fact.
        scores = [1 / (1 + math.exp(-float(score))) for score in raw_scores]
        ranked = sorted(zip(candidates, scores, raw_scores), key=lambda item: item[1], reverse=True)
        forced_ids = set(dependency_ids)
        if deterministic_plan:
            forced_ids.update(
                fact_id for fact_id in (
                    deterministic_plan["targetFactId"],
                    deterministic_plan["formulaFactId"],
                    deterministic_plan["baseFactId"],
                )
                if fact_id
            )
        trace["rerankedCandidates"] = [
            {**self._trace_fact(row), "rerankScore": score, "rawRerankLogit": raw_score, "passedCutoff": score >= 0.50, "selected": profile_request or (score >= 0.50 and rank <= 20) or row["id"] in forced_ids}
            for rank, (row, score, raw_score) in enumerate(ranked, 1)
        ]
        if profile_request:
            selected = [row for row, _, _ in ranked][:PROFILE_FACT_LIMIT]
        else:
            selected = [row for row, score, _ in ranked if score >= 0.50][:20]
            selected_ids = {row["id"] for row in selected}
            selected.extend(row for row, _, _ in ranked if row["id"] in forced_ids and row["id"] not in selected_ids)
        trace["formulaDependencyExpansion"] = dependency_trace
        trace["deterministicFormulaPlan"] = deterministic_plan
        trace["profileExpansion"] = profile_trace
        trace["selectedFactIds"] = [row["id"] for row in selected]
        return selected, trace

    @staticmethod
    def _is_profile_request(question: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", question.lower()))
        return bool(words & PROFILE_REQUEST_TERMS)

    def _profile_candidates(self, connection: sqlite3.Connection, question: str, seed_rows: list[sqlite3.Row], document_ids: list[str] | None) -> tuple[list[sqlite3.Row], dict[str, Any]]:
        """Expand an overview request from a person mention to their related facts.

        This does not use a banking field list. It finds documents whose fact
        values contain the identifying query terms, then keeps a bounded,
        de-duplicated set of page-backed facts from those documents.
        """
        query_terms = meaningful_terms(question)
        entity_terms = [term for term in query_terms if term not in PROFILE_GENERIC_TERMS]
        trace: dict[str, Any] = {"entityTerms": entity_terms, "documents": []}
        if not entity_terms:
            trace["reason"] = "No customer-identifying term was present in the overview request."
            return [], trace
        sql = "SELECT document_id, COUNT(*) AS matches FROM facts WHERE (" + " OR ".join("lower(raw_value) LIKE ?" for _ in entity_terms) + ")"
        params: list[Any] = [f"%{term.lower()}%" for term in entity_terms]
        if document_ids:
            sql += f" AND document_id IN ({','.join('?' for _ in document_ids)})"
            params.extend(document_ids)
        entity_documents = [row[0] for row in connection.execute(sql + " GROUP BY document_id ORDER BY matches DESC", params).fetchall()]
        if not entity_documents:
            trace["reason"] = "No documents contained the identifying query terms in extracted fact values."
            return [], trace
        # Prefer documents that ordinary retrieval already found relevant, then
        # retain the remaining entity-linked documents for profile completeness.
        seed_document_order = [row["document_id"] for row in seed_rows if row["document_id"] in entity_documents]
        ordered_documents = list(dict.fromkeys([*seed_document_order, *entity_documents]))
        rows = connection.execute(
            f"SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id WHERE facts.document_id IN ({','.join('?' for _ in ordered_documents)}) ORDER BY facts.page_number, facts.id",
            ordered_documents,
        ).fetchall()
        rows_by_document: dict[str, list[sqlite3.Row]] = {document_id: [] for document_id in ordered_documents}
        for row in rows:
            rows_by_document[row["document_id"]].append(row)
        profile_rows: list[sqlite3.Row] = []
        for document_id in ordered_documents:
            unique: list[sqlite3.Row] = []
            labels_seen: set[str] = set()
            for row in rows_by_document[document_id]:
                key = row["raw_label"].casefold().strip()
                if key in labels_seen:
                    continue
                labels_seen.add(key)
                unique.append(row)
            # First facts usually contain identity/context; trailing facts often
            # contain application declarations. Keeping both avoids page-order bias.
            selected = unique[:20] + unique[-20:]
            selected = list({row["id"]: row for row in selected}.values())[:PROFILE_FACTS_PER_DOCUMENT]
            profile_rows.extend(selected)
            filename = selected[0]["original_name"] if selected else None
            trace["documents"].append({"documentId": document_id, "filename": filename, "uniqueFactCount": len(unique), "selectedFactCount": len(selected)})
            if len(profile_rows) >= PROFILE_FACT_LIMIT:
                break
        return profile_rows[:PROFILE_FACT_LIMIT], trace

    def _formula_dependency_ids(self, connection: sqlite3.Connection, candidates: list[sqlite3.Row], document_ids: list[str] | None) -> tuple[list[str], list[dict[str, Any]]]:
        """Bring an explicit formula's input facts into the selector context.

        This is relationship expansion based on formula syntax, not a list of
        banking labels.  For ``12 times gross monthly income`` it searches for
        facts that contain all of ``gross``, ``monthly``, and ``income``.
        """
        dependency_ids: list[str] = []
        trace: list[dict[str, Any]] = []
        for row in candidates:
            match = MULTIPLIER_PATTERN.search(row["raw_value"])
            if not match:
                continue
            terms = meaningful_terms(row["raw_value"][match.end():])
            if not terms:
                continue
            sql = "SELECT facts.id FROM fact_fts JOIN facts ON facts.id=fact_fts.fact_id WHERE fact_fts MATCH ?"
            params: list[Any] = [" AND ".join(f'\"{term}\"' for term in terms)]
            if document_ids:
                sql += f" AND facts.document_id IN ({','.join('?' for _ in document_ids)})"
                params.extend(document_ids)
            found = [item[0] for item in connection.execute(sql + " ORDER BY bm25(fact_fts) LIMIT 12", params).fetchall() if item[0] != row["id"]]
            dependency_ids.extend(found)
            trace.append({"formulaFactId": row["id"], "expressionTerms": terms, "expandedFactIds": found})
        return list(dict.fromkeys(dependency_ids)), trace

    @staticmethod
    def _is_limit_request(question: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", question.lower()))
        return bool(words & LIMIT_REQUEST_TERMS)

    @classmethod
    def _deterministic_formula_plan(cls, question: str, facts: list[sqlite3.Row]) -> dict[str, Any] | None:
        """Find a safely calculable multiplier comparison without relying on field names.

        The route is deliberately narrow: it is available only for a question that
        expresses a limit/eligibility intent and evidence that contains an explicit
        multiplier. A target must be a numeric fact whose label overlaps the
        question, and a formula input must match the formula's own expression.
        Otherwise we decline and leave the normal evidence pipeline in control.
        """
        if not cls._is_limit_request(question):
            return None
        question_terms = set(meaningful_terms(question))
        formulas = [row for row in facts if MULTIPLIER_PATTERN.search(row["raw_value"])]
        numeric_facts = [row for row in facts if row["normalized_value"] is not None]
        best: tuple[int, sqlite3.Row, sqlite3.Row, sqlite3.Row] | None = None
        for formula in formulas:
            formula_terms = set(re.findall(r"[a-z0-9]{2,}", cls._fact_text(formula).lower()))
            formula_overlap = len(question_terms & formula_terms)
            if formula_overlap == 0:
                continue
            match = MULTIPLIER_PATTERN.search(formula["raw_value"])
            if not match:
                continue
            input_terms = set(meaningful_terms(formula["raw_value"][match.end():]))
            base_options = [
                (len(input_terms & set(re.findall(r"[a-z0-9]{2,}", cls._fact_text(row).lower()))), row)
                for row in numeric_facts
                if row["id"] != formula["id"]
            ]
            if not base_options:
                continue
            base_overlap, base = max(base_options, key=lambda item: item[0])
            if base_overlap == 0:
                continue
            target_options = []
            for row in numeric_facts:
                if row["id"] == base["id"]:
                    continue
                # Labels are compact, intentional descriptions of a value. Giving
                # them priority prevents a long evidence sentence from swamping a
                # precise numeric field match.
                label_terms = set(re.findall(r"[a-z0-9]{2,}", row["raw_label"].lower()))
                target_overlap = len(question_terms & label_terms)
                if target_overlap:
                    target_options.append((target_overlap, row))
            if not target_options:
                continue
            target_overlap, target = max(target_options, key=lambda item: item[0])
            confidence = (formula_overlap * 3) + (base_overlap * 2) + (target_overlap * 3)
            candidate = (confidence, target, formula, base)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            return None
        _, target, formula, base = best
        return {
            "operation": "formula_compare",
            "factIds": [target["id"], formula["id"], base["id"]],
            "targetFactId": target["id"],
            "formulaFactId": formula["id"],
            "baseFactId": base["id"],
            "comparison": "less_than_or_equal",
            "answer": "",
            "selectionMethod": "deterministic-explicit-formula",
        }

    def _resolve(self, question: str, facts: list[sqlite3.Row], profile_request: bool = False) -> dict[str, Any]:
        from anthropic import Anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
        context = "\n".join(
            f"[{row['id']}] type={row['value_type']}; currency={row['currency'] or 'none'}; {self._fact_text(row)}"
            for row in facts
        )
        message = Anthropic(api_key=key).messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=1200,
            system=(
                "Use only supplied facts. Select fact IDs and a generic operation. "
                "Never mix currencies. If a question asks whether one value is within, below, above, eligible under, "
                "or limited by a policy, and a supplied fact contains an explicit multiplier expression such as '12 times X', "
                "you MUST use formula_compare rather than fact_lookup or none. factIds must contain the target, formula, and base facts; "
                "set targetFactId to the value being tested, formulaFactId to the explicit policy formula, baseFactId to the formula input, "
                "and use less_than_or_equal when the question asks whether a value is within a limit. "
                "Do not reject a formula merely because it is written in words. "
                "When requestMode is profile, you MUST use profile. Write a concise customer overview in answer using only selected facts. "
                "Include identity, application, income/employment, and obligations only when evidence exists; mention a material conflict or missing evidence only when directly supported. "
                "Never claim approval, rejection, creditworthiness, or a policy decision unless a supplied fact states it. "
                "For all non-profile operations, leave answer empty because Python performs the final calculation."
            ),
            messages=[{"role": "user", "content": f"Request mode: {'profile' if profile_request else 'standard'}\nQuestion: {question}\nFacts:\n{context}"}],
            tools=[{"name": "select_facts", "description": "Select facts and a safe generic operation. For a policy formula such as '12 times monthly income', use formula_compare and provide targetFactId, formulaFactId, and baseFactId. For profile mode, return a sourced customer overview in answer.", "input_schema": {"type": "object", "properties": {"operation": {"type": "string", "enum": ["fact_lookup", "sum", "average", "minimum", "maximum", "count", "difference", "compare", "formula_compare", "profile", "none"]}, "factIds": {"type": "array", "items": {"type": "string"}, "maxItems": 24}, "answer": {"type": "string"}, "comparison": {"type": "string", "enum": ["less_than_or_equal", "less_than", "greater_than_or_equal", "greater_than", "equal"]}, "targetFactId": {"type": "string"}, "formulaFactId": {"type": "string"}, "baseFactId": {"type": "string"}}, "required": ["operation", "factIds", "answer"], "additionalProperties": False}}],
            tool_choice={"type": "tool", "name": "select_facts"},
        )
        return next((block.input for block in message.content if block.type == "tool_use"), {"operation": "none", "factIds": []})

    def _calculate(self, operation: str, facts: list[sqlite3.Row], plan: dict[str, Any]) -> tuple[str, list[sqlite3.Row]] | None:
        if operation == "count":
            return f"Count: {len(facts)} matching records.\n\nEvidence:\n{self._evidence_summary(facts)}", facts
        if operation == "formula_compare":
            return self._formula_compare(facts, plan)
        currencies = {row["currency"] for row in facts if row["currency"]}
        if len(currencies) > 1:
            return "I cannot calculate across multiple currencies without an approved exchange rate.", facts
        try:
            values = [Decimal(row["normalized_value"]) for row in facts if row["normalized_value"] is not None]
        except (InvalidOperation, TypeError):
            return None
        if not values:
            return None
        if operation == "sum": result, symbol = sum(values), "Total"
        elif operation == "average": result, symbol = sum(values) / len(values), "Average"
        elif operation == "minimum": result, symbol = min(values), "Minimum"
        elif operation == "maximum": result, symbol = max(values), "Maximum"
        elif operation == "difference" and len(values) == 2: result, symbol = values[0] - values[1], "Difference"
        elif operation == "compare" and len(values) == 2:
            comparison = plan.get("comparison", "less_than_or_equal")
            checks = {"less_than_or_equal": values[0] <= values[1], "less_than": values[0] < values[1], "greater_than_or_equal": values[0] >= values[1], "greater_than": values[0] > values[1], "equal": values[0] == values[1]}
            difference = abs(values[0] - values[1])
            conclusion = "The two values match exactly." if values[0] == values[1] else f"The absolute difference is {next(iter(currencies), '')} {difference}."
            return (
                f"Comparison:\n"
                f"- {facts[0]['raw_label']}: {facts[0]['raw_value']}\n"
                f"- {facts[1]['raw_label']}: {facts[1]['raw_value']}\n\n"
                f"Result: {facts[0]['raw_label']} is {'consistent with' if checks[comparison] else 'not consistent with'} "
                f"{comparison.replace('_', ' ')} {facts[1]['raw_label']}. {conclusion}\n\n"
                f"Evidence:\n{self._evidence_summary(facts)}",
                facts,
            )
        else: return None
        currency = next(iter(currencies), "")
        return f"{symbol}: {currency} {result}\n\nCalculated from:\n{self._evidence_summary(facts)}", facts

    @staticmethod
    def _formula_compare(facts: list[sqlite3.Row], plan: dict[str, Any]) -> tuple[str, list[sqlite3.Row]] | None:
        """Evaluate a simple, explicit multiplier policy without naming any business field.

        This deliberately recognises syntax (for example, ``12 times income``), not
        labels such as "loan amount" or "salary". More expressive policy language is
        retained as evidence and can be added as new rule parsers later.
        """
        by_id = {row["id"]: row for row in facts}
        target = by_id.get(plan.get("targetFactId"))
        formula = by_id.get(plan.get("formulaFactId"))
        base = by_id.get(plan.get("baseFactId"))
        # Older selector responses may contain the three fact IDs but not the
        # role fields. Infer roles only when the formula has one unambiguous
        # numeric input; otherwise decline rather than guessing.
        formula_rows = [row for row in facts if MULTIPLIER_PATTERN.search(row["raw_value"])]
        if formula is None and len(formula_rows) == 1:
            formula = formula_rows[0]
        numeric_rows = [row for row in facts if row is not formula and row["normalized_value"] is not None]
        if formula is not None and base is None:
            expression = re.sub(r"^.*?(?<![\w.])-?\d+(?:\.\d+)?\s*(?:times|x|\*)", "", formula["raw_value"], flags=re.I)
            terms = set(re.findall(r"[a-z0-9]{2,}", expression.lower()))
            scored = [
                (len(terms & set(re.findall(r"[a-z0-9]{2,}", FactEngine._fact_text(row).lower()))), row)
                for row in numeric_rows
            ]
            if scored:
                score, candidate = max(scored, key=lambda item: item[0])
                if score > 0:
                    base = candidate
        if target is None and base is not None:
            remaining = [row for row in numeric_rows if row["id"] != base["id"]]
            if len(remaining) == 1:
                target = remaining[0]
        if not target or not formula or not base:
            return None
        if not target["normalized_value"] or not base["normalized_value"]:
            return None
        match = re.search(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:times|x|\*)", formula["raw_value"], re.I)
        if not match:
            return None
        if target["currency"] and base["currency"] and target["currency"] != base["currency"]:
            return "I cannot apply this formula across different currencies without an approved exchange rate.", [target, formula, base]
        try:
            target_value = Decimal(target["normalized_value"])
            base_value = Decimal(base["normalized_value"])
            multiplier = Decimal(match.group(1))
        except (InvalidOperation, TypeError):
            return None
        limit = multiplier * base_value
        comparison = plan.get("comparison", "less_than_or_equal")
        checks = {
            "less_than_or_equal": target_value <= limit,
            "less_than": target_value < limit,
            "greater_than_or_equal": target_value >= limit,
            "greater_than": target_value > limit,
            "equal": target_value == limit,
        }
        if comparison not in checks:
            return None
        currency = target["currency"] or base["currency"] or ""
        verdict = "is within" if checks[comparison] and comparison in {"less_than_or_equal", "less_than"} else "meets" if checks[comparison] else "is not within" if comparison in {"less_than_or_equal", "less_than"} else "does not meet"
        evidence = [target, formula, base]
        return (
            f"Calculation:\n"
            f"- Value being assessed — {target['raw_label']}: {target['raw_value']}\n"
            f"- Policy rule — {formula['raw_label']}: {formula['raw_value']}\n"
            f"- Formula input — {base['raw_label']}: {base['raw_value']}\n"
            f"- Calculated limit: {currency} {limit} ({multiplier} × {base['raw_value']})\n\n"
            f"Result: {target['raw_label']} {verdict} the calculated limit.\n\n"
            f"Evidence:\n{FactEngine._evidence_summary(evidence)}",
            evidence,
        )

    @staticmethod
    def _has_required_numbers(answer: str, values: list[Decimal]) -> bool:
        answer_digits = re.sub(r"\D", "", answer)
        return all(re.sub(r"\D", "", format(value, "f")) in answer_digits for value in values)

    @classmethod
    def _is_valid_formula_narration(cls, answer: str, expected_within: bool, values: list[Decimal]) -> bool:
        """Reject prose that contradicts the locally verified calculation."""
        normalized = answer.strip().casefold()
        expected_opening = "yes" if expected_within else "no"
        if not re.match(rf"^{expected_opening}(?:[,.!\s]|$)", normalized):
            return False
        contradiction = (
            r"\b(?:no|not|isn't|isnt|doesn't|does not|cannot)\b"
            r"(?:\W+\w+){0,5}\W+\b(?:within|eligible|permissible|allowed)\b"
        )
        if expected_within and re.search(contradiction, normalized):
            return False
        return cls._has_required_numbers(answer, values)

    @staticmethod
    def _contains_fact_value(answer: str, raw_value: str) -> bool:
        normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        return normalize(raw_value) in normalize(answer)

    def _narrate_verified_fact(self, question: str, row: sqlite3.Row) -> str | None:
        """Make one selected fact readable without allowing new evidence."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        verified_fact = {"question": question, "label": row["raw_label"], "value": row["raw_value"]}
        try:
            from anthropic import Anthropic
            message = Anthropic(api_key=key).messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=180,
                system=(
                    "Answer the question naturally and concisely using only the one verified fact supplied. "
                    "State the fact's value exactly as supplied. Do not add any other fact, inference, citation, filename, page number, or heading."
                ),
                messages=[{"role": "user", "content": json.dumps(verified_fact)}],
                tools=[{
                    "name": "write_answer", "description": "Write a grounded natural-language answer.",
                    "input_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False},
                }], tool_choice={"type": "tool", "name": "write_answer"},
            )
            result = next((block.input for block in message.content if block.type == "tool_use"), {})
            answer = result.get("answer") if isinstance(result, dict) else None
            if not isinstance(answer, str) or not answer.strip() or not self._contains_fact_value(answer, row["raw_value"]):
                return None
            return answer.strip()
        except Exception:
            return None

    def _narrate_verified_formula(self, question: str, facts: list[sqlite3.Row], plan: dict[str, Any]) -> str | None:
        """Use Claude only as a constrained presentation layer for a verified calculation.

        Selection, arithmetic, currency checks, and citations have already been
        completed locally. The model receives those three facts only and cannot
        supply citations or choose different evidence. Its response is accepted
        only if it repeats every numeric input and the calculated limit.
        """
        by_id = {row["id"]: row for row in facts}
        target = by_id.get(plan.get("targetFactId"))
        formula = by_id.get(plan.get("formulaFactId"))
        base = by_id.get(plan.get("baseFactId"))
        if not target or not formula or not base:
            return None
        match = re.search(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:times|x|\*)", formula["raw_value"], re.I)
        if not match or not target["normalized_value"] or not base["normalized_value"]:
            return None
        try:
            target_value = Decimal(target["normalized_value"])
            base_value = Decimal(base["normalized_value"])
            limit = Decimal(match.group(1)) * base_value
        except InvalidOperation:
            return None
        comparison = plan.get("comparison", "less_than_or_equal")
        comparison_checks = {
            "less_than_or_equal": target_value <= limit,
            "less_than": target_value < limit,
            "greater_than_or_equal": target_value >= limit,
            "greater_than": target_value > limit,
            "equal": target_value == limit,
        }
        if comparison not in comparison_checks:
            return None
        expected_within = comparison_checks[comparison]
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        verified_data = {
            "question": question,
            "valueBeingAssessed": {"label": target["raw_label"], "displayValue": target["raw_value"], "normalizedValue": str(target_value)},
            "policyRule": {"label": formula["raw_label"], "text": formula["raw_value"]},
            "formulaInput": {"label": base["raw_label"], "displayValue": base["raw_value"], "normalizedValue": str(base_value)},
            "calculatedLimit": {"currency": target["currency"] or base["currency"] or None, "normalizedValue": str(limit)},
            "verifiedConclusion": "within the limit" if expected_within else "not within the limit",
        }
        try:
            from anthropic import Anthropic
            message = Anthropic(api_key=key).messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=280,
                system=(
                    "Write a concise, professional answer using only the verified calculation data supplied. "
                    f"The verified conclusion is: {verified_data['verifiedConclusion']}. "
                    f"You MUST begin the answer with {'Yes.' if expected_within else 'No.'} and never state the opposite conclusion. "
                    "Then naturally explain the assessed value, policy rule, formula input, "
                    "and calculated limit. Include every supplied number. Do not introduce any new fact, condition, "
                    "approval decision, citation, filename, page number, or caveat. Do not use headings or bullet lists."
                ),
                messages=[{"role": "user", "content": json.dumps(verified_data)}],
                tools=[{
                    "name": "write_answer",
                    "description": "Return the natural-language answer based solely on verified calculation data.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["within", "not_within"]},
                        },
                        "required": ["answer", "verdict"],
                        "additionalProperties": False,
                    },
                }],
                tool_choice={"type": "tool", "name": "write_answer"},
            )
            result = next((block.input for block in message.content if block.type == "tool_use"), {})
            answer = result.get("answer") if isinstance(result, dict) else None
            verdict = result.get("verdict") if isinstance(result, dict) else None
            expected_verdict = "within" if expected_within else "not_within"
            if not isinstance(answer, str) or verdict != expected_verdict:
                return None
            required_values = [target_value, base_value, limit]
            return answer.strip() if self._is_valid_formula_narration(answer, expected_within, required_values) else None
        except Exception:
            # Presentation must never make a verified calculation unavailable.
            return None

    @staticmethod
    def _pages(text: str) -> list[tuple[int, str]]:
        parts = re.split(r"^--- Page (\d+) ---\s*$", text, flags=re.MULTILINE)
        return [(int(parts[index]), parts[index + 1].strip()) for index in range(1, len(parts), 2)] or [(1, text)]

    @staticmethod
    def _fact_text(row: sqlite3.Row) -> str:
        return " | ".join(filter(None, [row["raw_label"], row["raw_value"], row["subject"], row["evidence_text"]]))

    @staticmethod
    def _trace_fact(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "filename": row["original_name"], "page": row["page_number"],
            "label": row["raw_label"], "value": row["raw_value"], "normalizedValue": row["normalized_value"],
            "type": row["value_type"], "currency": row["currency"], "evidence": row["evidence_text"][:1200],
        }

    @staticmethod
    def _evidence_summary(rows: list[sqlite3.Row]) -> str:
        return "\n".join(
            f"- {row['original_name'] if 'original_name' in row.keys() else 'Source document'}, "
            f"page {row['page_number'] if 'page_number' in row.keys() else 'unknown'}: "
            f"{row['raw_label']} = {row['raw_value']}"
            for row in rows
        )

    @staticmethod
    def _map_concepts(rows: list[sqlite3.Row]) -> dict[str, str]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured for concept mapping.")
        from anthropic import Anthropic
        source = [{"id": row["id"], "label": row["raw_label"], "value": row["raw_value"], "page": row["page_number"]} for row in rows]
        message = Anthropic(api_key=key).messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=1600,
            system=(
                "Map every supplied document fact to the most specific canonical concept from the allowed ontology. "
                "Use none when a fact does not fit. Do not infer values or change labels. "
                "Distinguish an employer/company name from its address and code; distinguish employee ID from customer ID; "
                "and distinguish a person's residential address from an employer address."
            ),
            messages=[{"role": "user", "content": json.dumps(source)}],
            tools=[{
                "name": "map_facts", "description": "Map fact IDs to canonical concepts.",
                "input_schema": {
                    "type": "object", "properties": {
                        "mappings": {"type": "array", "items": {"type": "object", "properties": {"factId": {"type": "string"}, "concept": {"type": "string", "enum": CANONICAL_CONCEPTS}}, "required": ["factId", "concept"], "additionalProperties": False}},
                    }, "required": ["mappings"], "additionalProperties": False,
                },
            }], tool_choice={"type": "tool", "name": "map_facts"},
        )
        result = next((block.input for block in message.content if block.type == "tool_use"), {"mappings": []})
        valid_ids = {row["id"] for row in rows}
        return {
            item["factId"]: item["concept"]
            for item in result.get("mappings", []) if isinstance(item, dict)
            and item.get("factId") in valid_ids and item.get("concept") in CANONICAL_CONCEPTS
            and item.get("concept") != "none"
        } if isinstance(result, dict) else {}

    @staticmethod
    def _validate_fact(fact: Any) -> dict[str, Any] | None:
        if not isinstance(fact, dict) or not all(isinstance(fact.get(key), str) and fact[key].strip() for key in ("rawLabel", "rawValue", "valueType", "evidenceText")):
            return None
        value = fact.get("normalizedValue")
        if value is not None:
            try: value = str(Decimal(str(value)))
            except InvalidOperation: value = None
        try:
            confidence = min(max(float(fact.get("confidence", 0)), 0), 1)
            page_number = max(int(fact.get("pageNumber", 1)), 1)
        except (TypeError, ValueError):
            confidence, page_number = 0, 1
        concept = str(fact.get("canonicalConcept") or "").strip()
        return {"rawLabel": fact["rawLabel"].strip()[:200], "rawValue": fact["rawValue"].strip()[:200], "normalizedValue": value, "valueType": fact["valueType"].strip()[:60], "currency": str(fact.get("currency") or "").upper()[:3] or None, "period": str(fact.get("period") or "").strip()[:60] or None, "subject": str(fact.get("subject") or "").strip()[:160] or None, "canonicalConcept": concept if concept in CANONICAL_CONCEPTS and concept != "none" else None, "evidenceText": fact["evidenceText"].strip()[:1200], "confidence": confidence, "pageNumber": page_number}

    def _extract_page(self, page_number: int, page_text: str) -> list[dict[str, Any]]:
        from anthropic import Anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key: raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
        schema = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object", "properties": {"rawLabel": {"type": "string"}, "rawValue": {"type": "string"}}, "required": ["rawLabel", "rawValue"], "additionalProperties": False}}}, "required": ["facts"], "additionalProperties": False}
        message = Anthropic(api_key=key).messages.create(model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=1200, system="You are a high-recall document fact extractor. You MUST extract every visible labelled fact, including names, identifiers, amounts, dates, durations, and table values. Do not invent values and do not return an empty list when labelled values exist.", messages=[{"role": "user", "content": f"Extract labelled facts from page {page_number}:\n{page_text}"}], tools=[{"name": "store_facts", "description": "Store every visible labelled fact.", "input_schema": schema}], tool_choice={"type": "tool", "name": "store_facts"})
        result = next((block.input for block in message.content if block.type == "tool_use"), {"facts": []})
        raw_facts = result.get("facts", []) if isinstance(result, dict) else []
        return [self._enrich_fact(item, page_number) for item in raw_facts if isinstance(item, dict)]

    @staticmethod
    def _enrich_fact(fact: dict[str, Any], page_number: int) -> dict[str, Any]:
        raw_value = str(fact.get("rawValue") or "").strip()
        currency = "INR" if "₹" in raw_value else next((code for code in ("USD", "EUR", "GBP", "AED") if code in raw_value.upper()), None)
        number = re.search(r"-?[\d][\d,]*(?:\.\d+)?", raw_value)
        normalized = number.group(0).replace(",", "") if number and (currency or re.fullmatch(r"-?[\d][\d,]*(?:\.\d+)?", raw_value)) else None
        is_multiplier_formula = bool(re.search(r"(?<![\w.])-?\d+(?:\.\d+)?\s*(?:times|x|\*)", raw_value, re.I))
        value_type = "money" if currency else "formula" if is_multiplier_formula else "percentage" if "%" in raw_value else "date" if re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", raw_value) else "duration" if re.search(r"\b(month|year|day)s?\b", raw_value, re.I) else "number" if normalized else "text"
        return {"rawLabel": fact.get("rawLabel", ""), "rawValue": raw_value, "normalizedValue": normalized, "valueType": value_type, "currency": currency, "period": "monthly" if re.search(r"monthly|per month", str(fact.get("rawLabel", "")), re.I) else None, "subject": None, "evidenceText": f"{fact.get('rawLabel', '')}: {raw_value}", "confidence": 0.8, "pageNumber": page_number}

    def _collection_for(self) -> Any:
        if self._collection is None:
            import chromadb
            self._collection = chromadb.PersistentClient(path=str(FACT_VECTOR_DIR)).get_or_create_collection("document_facts")
        return self._collection
