"""Knowledge spaces: which governed context a document belongs to.

Every document belongs to exactly one knowledge context:

* Entity knowledge - customer/account specific documents. A document is entity
  knowledge unless it has been filed into a shared space; its owner is the
  customer recorded in ``document_entity_links`` (automatic resolution or a
  reviewer's assignment).
* Reference knowledge - shared policies, regulations, product rules.
* Operational knowledge - shared SOPs, checklists, escalation guides.

Shared documents are filed into a named space (for example "Lending Policies").
They are never linked to a customer, so they can never leak into another
customer's entity scope, and customer documents never appear in a shared space.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

ENTITY = "entity"
REFERENCE = "reference"
OPERATIONAL = "operational"
SHARED_KINDS = {REFERENCE, OPERATIONAL}

KIND_LABELS = {
    ENTITY: "Entity knowledge",
    REFERENCE: "Reference knowledge",
    OPERATIONAL: "Operational knowledge",
}

DEFAULT_SPACES = [
    ("lending-policies", REFERENCE, "Lending Policies",
     "Personal loan product rules, eligibility criteria and lending limits."),
    ("compliance-regulatory", REFERENCE, "Compliance & Regulatory",
     "KYC, AML and regulatory guidance that applies to every customer."),
    ("loan-operations", OPERATIONAL, "Loan Review Procedures",
     "Loan review SOPs, manual review checklists and escalation guides."),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialise(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS knowledge_spaces (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    # Only shared documents have a row. No row means entity knowledge.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS document_spaces (
            document_id TEXT PRIMARY KEY, space_id TEXT NOT NULL, filed_at TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES uploads(id),
            FOREIGN KEY(space_id) REFERENCES knowledge_spaces(id)
        )"""
    )
    connection.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    # Seed once, so a space the user deletes is not recreated on restart.
    if connection.execute("SELECT 1 FROM app_meta WHERE key='knowledge_seeded_v1'").fetchone() is None:
        now = utc_now()
        for space_id, kind, name, description in DEFAULT_SPACES:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_spaces(id, kind, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (space_id, kind, name, description, now, now),
            )
        connection.execute("INSERT INTO app_meta(key, value) VALUES ('knowledge_seeded_v1', ?)", (now,))


def space_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "kind": row["kind"], "kindLabel": KIND_LABELS[row["kind"]],
        "name": row["name"], "description": row["description"],
        "documentCount": row["document_count"] if "document_count" in row.keys() else 0,
    }


def list_spaces(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT s.*, COUNT(d.document_id) AS document_count
        FROM knowledge_spaces s LEFT JOIN document_spaces d ON d.space_id=s.id
        GROUP BY s.id ORDER BY CASE s.kind WHEN 'reference' THEN 0 ELSE 1 END, lower(s.name)"""
    ).fetchall()
    return [space_payload(row) for row in rows]


def get_space(connection: sqlite3.Connection, space_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM document_spaces d WHERE d.space_id=s.id) AS document_count
        FROM knowledge_spaces s WHERE s.id=?""",
        (space_id,),
    ).fetchone()


def create_space(connection: sqlite3.Connection, kind: str, name: str, description: str) -> str:
    if kind not in SHARED_KINDS:
        raise ValueError("Shared spaces must be reference or operational knowledge.")
    space_id = str(uuid.uuid4())
    now = utc_now()
    connection.execute(
        "INSERT INTO knowledge_spaces(id, kind, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (space_id, kind, name, description, now, now),
    )
    return space_id


def shared_space_for_document(connection: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT s.* FROM document_spaces d JOIN knowledge_spaces s ON s.id=d.space_id
        WHERE d.document_id=?""",
        (document_id,),
    ).fetchone()


def file_into_space(connection: sqlite3.Connection, document_id: str, space_id: str) -> None:
    """File a document as shared knowledge and remove any customer ownership.

    A policy or SOP is not customer evidence: identifiers extracted from it must
    not remain on a customer, and it must not be returned in a customer scope.
    """
    if get_space(connection, space_id) is None:
        raise ValueError("Knowledge space not found.")
    connection.execute("DELETE FROM entity_identifiers WHERE source_document_id=?", (document_id,))
    connection.execute("DELETE FROM document_entity_links WHERE document_id=?", (document_id,))
    connection.execute(
        """INSERT INTO document_spaces(document_id, space_id, filed_at) VALUES (?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET space_id=excluded.space_id, filed_at=excluded.filed_at""",
        (document_id, space_id, utc_now()),
    )


def move_to_entity_knowledge(connection: sqlite3.Connection, document_id: str) -> None:
    """Return a shared document to customer knowledge; ownership is re-resolved."""
    connection.execute("DELETE FROM document_spaces WHERE document_id=?", (document_id,))
    ready = connection.execute("SELECT 1 FROM document_facts WHERE upload_id=? AND status='ready'", (document_id,)).fetchone()
    if ready is not None:
        connection.execute(
            """INSERT INTO document_entity_links(document_id, entity_id, status, confidence, reason, resolved_at)
            VALUES (?, NULL, 'queued', NULL, NULL, NULL)
            ON CONFLICT(document_id) DO UPDATE SET entity_id=NULL, status='queued', confidence=NULL, reason=NULL, resolved_at=NULL""",
            (document_id,),
        )


def delete_document(connection: sqlite3.Connection, document_id: str) -> None:
    connection.execute("DELETE FROM document_spaces WHERE document_id=?", (document_id,))


def space_document_ids(connection: sqlite3.Connection, space_ids: list[str]) -> list[str]:
    if not space_ids:
        return []
    placeholders = ",".join("?" for _ in space_ids)
    return [
        row[0] for row in connection.execute(
            f"SELECT document_id FROM document_spaces WHERE space_id IN ({placeholders}) ORDER BY document_id",
            space_ids,
        ).fetchall()
    ]


def entity_document_ids(connection: sqlite3.Connection, entity_id: str) -> list[str]:
    """A customer's own documents, excluding anything filed as shared knowledge."""
    return [
        row[0] for row in connection.execute(
            """SELECT l.document_id FROM document_entity_links l
            WHERE l.entity_id=? AND l.status='linked'
              AND NOT EXISTS (SELECT 1 FROM document_spaces d WHERE d.document_id=l.document_id)
            ORDER BY l.document_id""",
            (entity_id,),
        ).fetchall()
    ]
