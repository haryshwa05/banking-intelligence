from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable


STRONG_IDENTIFIER_KINDS = {"customer_id", "application_id", "government_id", "account_number", "email", "phone"}
IDENTIFIER_KINDS = ["customer_id", "application_id", "government_id", "account_number", "email", "phone", "full_name", "date_of_birth"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EntityResolver:
    """Links documents to persistent customers using provenance-backed identifiers."""

    def __init__(self, identity_extractor: Callable[[list[sqlite3.Row]], dict[str, Any]] | None = None) -> None:
        self._identity_extractor = identity_extractor

    def initialise(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, display_name TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS entity_identifiers (
                id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
                normalized_value TEXT NOT NULL, display_value TEXT NOT NULL,
                source_document_id TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(entity_id) REFERENCES entities(id),
                FOREIGN KEY(source_document_id) REFERENCES uploads(id),
                UNIQUE(entity_id, kind, normalized_value, source_document_id)
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS entity_identifier_lookup ON entity_identifiers(kind, normalized_value)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS document_entity_links (
                document_id TEXT PRIMARY KEY, entity_id TEXT, status TEXT NOT NULL,
                confidence REAL, reason TEXT, resolved_at TEXT,
                FOREIGN KEY(document_id) REFERENCES uploads(id), FOREIGN KEY(entity_id) REFERENCES entities(id)
            )"""
        )

    def queue_document(self, connection: sqlite3.Connection, document_id: str) -> None:
        connection.execute(
            """INSERT INTO document_entity_links(document_id, entity_id, status, confidence, reason, resolved_at)
            VALUES (?, NULL, 'queued', NULL, NULL, NULL)
            ON CONFLICT(document_id) DO UPDATE SET entity_id=NULL, status='queued', confidence=NULL, reason=NULL, resolved_at=NULL""",
            (document_id,),
        )

    def resolve_document(self, connection: sqlite3.Connection, document_id: str) -> None:
        self.queue_document(connection, document_id)
        connection.execute("UPDATE document_entity_links SET status='processing' WHERE document_id=?", (document_id,))
        facts = connection.execute(
            "SELECT raw_label, raw_value, evidence_text, page_number FROM facts WHERE document_id=? ORDER BY page_number, id",
            (document_id,),
        ).fetchall()
        try:
            identity = self._extract_identity(facts)
            identifiers = self._validated_identifiers(identity.get("identifiers", []))
            display_name = str(identity.get("displayName") or self._name_value(identifiers) or "").strip()[:160] or None
            self._link_identity(connection, document_id, display_name, identifiers)
        except Exception as error:
            connection.execute(
                "UPDATE document_entity_links SET status='failed', reason=?, resolved_at=? WHERE document_id=?",
                (str(error)[:500], utc_now(), document_id),
            )

    def delete_document(self, connection: sqlite3.Connection, document_id: str) -> None:
        connection.execute("DELETE FROM document_entity_links WHERE document_id=?", (document_id,))
        connection.execute("DELETE FROM entity_identifiers WHERE source_document_id=?", (document_id,))

    def list_entities(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT e.id, e.display_name, COUNT(l.document_id) AS document_count
            FROM entities e LEFT JOIN document_entity_links l
              ON l.entity_id=e.id AND l.status='linked'
            GROUP BY e.id ORDER BY lower(e.display_name), e.id"""
        ).fetchall()
        return [{"id": row["id"], "name": row["display_name"] or "Unnamed customer", "documentCount": row["document_count"]} for row in rows]

    def create_entity(self, connection: sqlite3.Connection, name: str) -> str:
        entity_id = str(uuid.uuid4())
        now = utc_now()
        connection.execute(
            "INSERT INTO entities(id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (entity_id, name, now, now),
        )
        return entity_id

    def assign_document(self, connection: sqlite3.Connection, document_id: str, entity_id: str) -> None:
        entity = connection.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone()
        if entity is None:
            raise ValueError("Customer not found.")
        # Identifiers extracted for an earlier automatic link cannot remain on
        # that customer after a reviewer changes the document's ownership.
        connection.execute("DELETE FROM entity_identifiers WHERE source_document_id=?", (document_id,))
        connection.execute(
            """INSERT INTO document_entity_links(document_id, entity_id, status, confidence, reason, resolved_at)
            VALUES (?, ?, 'linked', 1.0, 'Manually assigned to this customer.', ?)
            ON CONFLICT(document_id) DO UPDATE SET entity_id=excluded.entity_id,
                status=excluded.status, confidence=excluded.confidence,
                reason=excluded.reason, resolved_at=excluded.resolved_at""",
            (document_id, entity_id, utc_now()),
        )

    def linked_document_ids(self, connection: sqlite3.Connection, entity_id: str) -> list[str] | None:
        if connection.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None:
            return None
        return [
            row[0] for row in connection.execute(
                "SELECT document_id FROM document_entity_links WHERE entity_id=? AND status='linked' ORDER BY document_id",
                (entity_id,),
            ).fetchall()
        ]

    def question_scope(self, connection: sqlite3.Connection, question: str) -> dict[str, Any]:
        """Resolve an explicitly named customer to linked documents for retrieval.

        This is intentionally conservative. An exact stored full name must occur
        in the question and identify one entity. A partial/ambiguous name leaves
        the question global rather than risking a cross-customer answer.
        """
        normalized_question = re.sub(r"[^a-z0-9]", "", question.lower())
        rows = connection.execute(
            """SELECT entities.id, entities.display_name, entity_identifiers.display_value
            FROM entities LEFT JOIN entity_identifiers
            ON entity_identifiers.entity_id=entities.id AND entity_identifiers.kind='full_name'"""
        ).fetchall()
        matches: dict[str, tuple[int, str]] = {}
        for row in rows:
            candidate_name = str(row["display_value"] or row["display_name"] or "").strip()
            normalized_name = re.sub(r"[^a-z0-9]", "", candidate_name.lower())
            if len(normalized_name) < 6 or normalized_name not in normalized_question:
                continue
            current = matches.get(row["id"])
            if current is None or len(normalized_name) > current[0]:
                matches[row["id"]] = (len(normalized_name), candidate_name)
        if not matches:
            return {"mode": "global", "reason": "No unique stored customer name was found in the question."}
        ordered = sorted(matches.items(), key=lambda item: item[1][0], reverse=True)
        if len(ordered) > 1 and ordered[0][1][0] == ordered[1][1][0]:
            return {"mode": "ambiguous", "reason": "More than one customer matched the name in the question."}
        entity_id, (_, entity_name) = ordered[0]
        document_ids = self.linked_document_ids(connection, entity_id) or []
        return {
            "mode": "entity", "entityId": entity_id, "entityName": entity_name,
            "documentIds": document_ids,
        }

    def _link_identity(self, connection: sqlite3.Connection, document_id: str, display_name: str | None, identifiers: list[dict[str, str]]) -> None:
        stable = [item for item in identifiers if item["kind"] in STRONG_IDENTIFIER_KINDS]
        candidate_scores: dict[str, int] = defaultdict(int)
        matched_kinds: dict[str, set[str]] = defaultdict(set)
        for item in identifiers:
            rows = connection.execute(
                "SELECT entity_id FROM entity_identifiers WHERE kind=? AND normalized_value=?",
                (item["kind"], item["normalizedValue"]),
            ).fetchall()
            weight = 100 if item["kind"] in STRONG_IDENTIFIER_KINDS else 30
            for row in rows:
                candidate_scores[row[0]] += weight
                matched_kinds[row[0]].add(item["kind"])
        ranked = sorted(candidate_scores.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            self._set_review(connection, document_id, "Conflicting identifiers match more than one customer.")
            return
        if ranked:
            entity_id, score = ranked[0]
            # One stable identifier, or the safer name + date-of-birth composite,
            # is required to attach to an existing customer.
            is_safe_match = bool(matched_kinds[entity_id] & STRONG_IDENTIFIER_KINDS) or {"full_name", "date_of_birth"}.issubset(matched_kinds[entity_id])
            if is_safe_match:
                self._store_link(connection, document_id, entity_id, "linked", min(score / 100, 1), "Matched existing customer identifiers.")
                self._store_identifiers(connection, entity_id, document_id, identifiers)
                if display_name:
                    connection.execute("UPDATE entities SET display_name=COALESCE(display_name, ?), updated_at=? WHERE id=?", (display_name, utc_now(), entity_id))
                return
            self._set_review(connection, document_id, "A name-only match was found; it was not linked automatically.")
            return
        if not stable:
            self._set_review(connection, document_id, "No stable customer identifier was found. Name-only evidence is not linked automatically.")
            return
        entity_id = str(uuid.uuid4())
        now = utc_now()
        connection.execute("INSERT INTO entities(id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)", (entity_id, display_name, now, now))
        self._store_identifiers(connection, entity_id, document_id, identifiers)
        self._store_link(connection, document_id, entity_id, "linked", 1.0, "Created a customer from stable document identifiers.")

    @staticmethod
    def _store_link(connection: sqlite3.Connection, document_id: str, entity_id: str, status: str, confidence: float, reason: str) -> None:
        connection.execute(
            "UPDATE document_entity_links SET entity_id=?, status=?, confidence=?, reason=?, resolved_at=? WHERE document_id=?",
            (entity_id, status, confidence, reason, utc_now(), document_id),
        )

    @staticmethod
    def _set_review(connection: sqlite3.Connection, document_id: str, reason: str) -> None:
        connection.execute(
            "UPDATE document_entity_links SET entity_id=NULL, status='needs_review', confidence=NULL, reason=?, resolved_at=? WHERE document_id=?",
            (reason, utc_now(), document_id),
        )

    @staticmethod
    def _store_identifiers(connection: sqlite3.Connection, entity_id: str, document_id: str, identifiers: list[dict[str, str]]) -> None:
        now = utc_now()
        for item in identifiers:
            connection.execute(
                """INSERT OR IGNORE INTO entity_identifiers(id, entity_id, kind, normalized_value, display_value, source_document_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), entity_id, item["kind"], item["normalizedValue"], item["value"], document_id, now),
            )

    def _extract_identity(self, facts: list[sqlite3.Row]) -> dict[str, Any]:
        if self._identity_extractor is not None:
            return self._identity_extractor(facts)
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured for entity resolution.")
        from anthropic import Anthropic
        source = [{"label": row["raw_label"], "value": row["raw_value"], "page": row["page_number"]} for row in facts]
        message = Anthropic(api_key=key).messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), max_tokens=500,
            system=(
                "Extract only the primary customer identity evidence explicitly present in the supplied document facts. "
                "Do not infer an identifier, do not merge people, and do not treat an employer or bank as the customer. "
                "Use identifiers exactly as written. A document can remain unidentified."
            ),
            messages=[{"role": "user", "content": json.dumps(source)}],
            tools=[{
                "name": "store_identity",
                "description": "Store explicit primary-customer identity evidence from one document.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "displayName": {"type": "string"},
                        "identifiers": {"type": "array", "items": {"type": "object", "properties": {"kind": {"type": "string", "enum": IDENTIFIER_KINDS}, "value": {"type": "string"}}, "required": ["kind", "value"], "additionalProperties": False}},
                    },
                    "required": ["identifiers"], "additionalProperties": False,
                },
            }], tool_choice={"type": "tool", "name": "store_identity"},
        )
        return next((block.input for block in message.content if block.type == "tool_use"), {"identifiers": []})

    @staticmethod
    def _validated_identifiers(items: Any) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("kind") not in IDENTIFIER_KINDS:
                continue
            value = str(item.get("value") or "").strip()[:200]
            normalized = EntityResolver._normalize(str(item["kind"]), value)
            if not normalized or (item["kind"], normalized) in seen:
                continue
            seen.add((item["kind"], normalized))
            result.append({"kind": str(item["kind"]), "value": value, "normalizedValue": normalized})
        return result

    @staticmethod
    def _normalize(kind: str, value: str) -> str:
        if kind == "full_name":
            return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", value.lower())).strip()
        if kind == "email":
            return value.lower().strip()
        return re.sub(r"[^a-z0-9]", "", value.lower())

    @staticmethod
    def _name_value(identifiers: list[dict[str, str]]) -> str | None:
        return next((item["value"] for item in identifiers if item["kind"] == "full_name"), None)
