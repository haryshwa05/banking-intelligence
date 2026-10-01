"""Specialised agents with governed knowledge access and capabilities.

An agent is reusable: it never stores a customer. A conversation binds one
agent to (optionally) one customer, and the agent's *effective knowledge* is
resolved for every turn from three things only:

    the selected customer's entity knowledge (if the agent may use it)
  + the shared spaces the agent is granted
  = the only documents retrieval and fact lookup may read.

Capabilities gate which existing engine routes may answer a question, so two
agents over the same documents can genuinely behave differently.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import knowledge

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Only Haiku is approved for API calls right now. Add a model here only once its
# use has been approved; rag.generation_options enforces the same allow-list.
MODELS = [
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "description": "Fast and economical. Good for lookups and routine questions."},
]
MODEL_IDS = {item["id"] for item in MODELS}

CAPABILITIES = [
    {"id": "document_search", "label": "Search document passages",
     "description": "Finds relevant passages in the permitted documents and answers with page citations."},
    {"id": "fact_lookup", "label": "Look up extracted facts",
     "description": "Answers from verified, page-backed facts such as names, employers, amounts and dates."},
    {"id": "calculations", "label": "Verified calculations",
     "description": "Totals, averages, differences and comparisons computed exactly, never estimated by the AI."},
    {"id": "policy_evaluation", "label": "Policy rule evaluation",
     "description": "Applies explicit policy formulas (for example '12 times gross monthly income') to customer values."},
    {"id": "customer_profile", "label": "Customer overview",
     "description": "Summarises a customer from the facts in their documents."},
    {"id": "consistency_check", "label": "Cross-document consistency check",
     "description": "Flags the same fact (name, date of birth, employer, income...) recorded differently across a customer's documents."},
]
CAPABILITY_IDS = {item["id"] for item in CAPABILITIES}
CUSTOMER_ACCESS = {"required", "optional", "none"}

BUILTIN_AGENTS = [
    {
        "id": "customer-document-analyst", "name": "Customer Document Analyst",
        "purpose": "Understands one customer's documents and finds facts and inconsistencies.",
        "description": "Works only inside the selected customer's own documents. Use it to find facts, get an overview of the customer, and spot values that disagree between documents.",
        "instructions": "Act as a meticulous document analyst. Report what the customer's documents state, quote exact values, and point out any disagreement between documents. Do not apply lending or compliance policy.",
        "customerAccess": "required", "spaceIds": [],
        "capabilities": ["document_search", "fact_lookup", "calculations", "customer_profile", "consistency_check"],
    },
    {
        "id": "loan-eligibility-analyst", "name": "Loan Eligibility Analyst",
        "purpose": "Evaluates a customer's loan eligibility against personal loan policy.",
        "description": "Combines the selected customer's documents with the Lending Policies space to evaluate eligibility conditions such as income multiples and loan limits.",
        "instructions": "Act as a loan eligibility analyst. Relate customer values to the specific policy conditions they are tested against. State clearly which conditions are met, not met, or cannot be evaluated from the evidence. Never state a final credit approval.",
        "customerAccess": "required", "spaceIds": ["lending-policies"],
        "capabilities": ["document_search", "fact_lookup", "calculations", "policy_evaluation", "customer_profile"],
    },
    {
        "id": "compliance-analyst", "name": "Compliance Analyst",
        "purpose": "Answers KYC, AML and regulatory questions for a customer or in general.",
        "description": "Uses the Compliance & Regulatory space, plus the selected customer's documents when a customer is chosen. Without a customer it answers general policy questions.",
        "instructions": "Act as a compliance analyst. Reference the specific KYC/AML requirement that applies, and identify missing, expired or inconsistent identity evidence. Do not make a final compliance determination; recommend escalation where the evidence is insufficient.",
        "customerAccess": "optional", "spaceIds": ["compliance-regulatory"],
        "capabilities": ["document_search", "fact_lookup", "customer_profile", "consistency_check"],
    },
]

CONSISTENCY_TERMS = re.compile(r"\b(inconsisten\w*|consistent|mismatch\w*|discrepan\w*|conflict\w*|contradict\w*|differ\w*|disagree\w*|match(?:es)?)\b", re.I)
# Values that legitimately vary between documents are not flagged.
CONSISTENCY_EXCLUDED_PREFIXES = ("policy.",)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialise(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL, customer_access TEXT NOT NULL,
            space_ids TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '[]',
            is_builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    if connection.execute("SELECT 1 FROM app_meta WHERE key='agents_seeded_v1'").fetchone() is None:
        now = utc_now()
        for agent in BUILTIN_AGENTS:
            connection.execute(
                """INSERT OR IGNORE INTO agents(id, name, purpose, description, instructions, model, customer_access,
                space_ids, capabilities, is_builtin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (agent["id"], agent["name"], agent["purpose"], agent["description"], agent["instructions"], DEFAULT_MODEL,
                 agent["customerAccess"], json.dumps(agent["spaceIds"]), json.dumps(agent["capabilities"]), now, now),
            )
        connection.execute("INSERT INTO app_meta(key, value) VALUES ('agents_seeded_v1', ?)", (now,))


def catalog() -> dict[str, Any]:
    return {"models": MODELS, "capabilities": CAPABILITIES, "defaultModel": DEFAULT_MODEL}


def agent_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "purpose": row["purpose"], "description": row["description"],
        "instructions": row["instructions"], "model": row["model"], "customerAccess": row["customer_access"],
        "spaceIds": json.loads(row["space_ids"] or "[]"), "capabilities": json.loads(row["capabilities"] or "[]"),
        "builtIn": bool(row["is_builtin"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "conversationCount": row["conversation_count"] if "conversation_count" in row.keys() else 0,
    }


def list_agents(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT a.*, (SELECT COUNT(*) FROM conversations c WHERE c.agent_id=a.id) AS conversation_count
        FROM agents a ORDER BY a.is_builtin DESC, lower(a.name)"""
    ).fetchall()
    return [agent_payload(row) for row in rows]


def get_agent(connection: sqlite3.Connection, agent_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT a.*, (SELECT COUNT(*) FROM conversations c WHERE c.agent_id=a.id) AS conversation_count
        FROM agents a WHERE a.id=?""",
        (agent_id,),
    ).fetchone()


def validate(connection: sqlite3.Connection, payload: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
    """Return a clean agent definition or raise ValueError with a user-facing message."""
    def text(key: str, minimum: int, maximum: int, label: str) -> str:
        value = payload.get(key, "")
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum} characters.")
        return value.strip()

    name = text("name", 2, 80, "Agent name")
    duplicate = connection.execute("SELECT id FROM agents WHERE lower(name)=lower(?) AND id IS NOT ?", (name, agent_id)).fetchone()
    if duplicate is not None:
        raise ValueError("Another agent already uses this name.")
    purpose = text("purpose", 0, 160, "Purpose")
    description = text("description", 0, 1000, "Description")
    instructions = text("instructions", 0, 4000, "Instructions")
    model = payload.get("model") or DEFAULT_MODEL
    if model not in MODEL_IDS:
        raise ValueError("Choose one of the available models.")
    customer_access = payload.get("customerAccess")
    if customer_access not in CUSTOMER_ACCESS:
        raise ValueError("Choose whether the agent uses customer documents.")
    space_ids = payload.get("spaceIds", [])
    if not isinstance(space_ids, list) or not all(isinstance(item, str) for item in space_ids):
        raise ValueError("spaceIds must be a list of knowledge space IDs.")
    space_ids = list(dict.fromkeys(space_ids))
    if space_ids:
        placeholders = ",".join("?" for _ in space_ids)
        found = {row[0] for row in connection.execute(f"SELECT id FROM knowledge_spaces WHERE id IN ({placeholders})", space_ids)}
        if found != set(space_ids):
            raise ValueError("One or more knowledge spaces no longer exist.")
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, list) or not capabilities or not set(capabilities) <= CAPABILITY_IDS:
        raise ValueError("Choose at least one capability from the list.")
    if customer_access == "none" and not space_ids:
        raise ValueError("An agent needs access to customer documents or at least one shared knowledge space.")
    return {
        "name": name, "purpose": purpose, "description": description, "instructions": instructions,
        "model": model, "customerAccess": customer_access, "spaceIds": space_ids,
        "capabilities": [item["id"] for item in CAPABILITIES if item["id"] in capabilities],
    }


def create_agent(connection: sqlite3.Connection, definition: dict[str, Any]) -> str:
    agent_id = str(uuid.uuid4())
    now = utc_now()
    connection.execute(
        """INSERT INTO agents(id, name, purpose, description, instructions, model, customer_access, space_ids,
        capabilities, is_builtin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (agent_id, definition["name"], definition["purpose"], definition["description"], definition["instructions"],
         definition["model"], definition["customerAccess"], json.dumps(definition["spaceIds"]),
         json.dumps(definition["capabilities"]), now, now),
    )
    return agent_id


def update_agent(connection: sqlite3.Connection, agent_id: str, definition: dict[str, Any]) -> None:
    connection.execute(
        """UPDATE agents SET name=?, purpose=?, description=?, instructions=?, model=?, customer_access=?,
        space_ids=?, capabilities=?, updated_at=? WHERE id=?""",
        (definition["name"], definition["purpose"], definition["description"], definition["instructions"],
         definition["model"], definition["customerAccess"], json.dumps(definition["spaceIds"]),
         json.dumps(definition["capabilities"]), utc_now(), agent_id),
    )


def remove_space_from_agents(connection: sqlite3.Connection, space_id: str) -> None:
    for row in connection.execute("SELECT id, space_ids FROM agents").fetchall():
        space_ids = json.loads(row["space_ids"] or "[]")
        if space_id in space_ids:
            connection.execute(
                "UPDATE agents SET space_ids=?, updated_at=? WHERE id=?",
                (json.dumps([item for item in space_ids if item != space_id]), utc_now(), row["id"]),
            )


def resolve_knowledge(connection: sqlite3.Connection, agent: sqlite3.Row, entity_id: str | None) -> dict[str, Any]:
    """Resolve the exact documents an agent may read for one customer context.

    Raises ValueError when the customer context is not valid for this agent.
    The result is computed from the agent's *current* definition, so revoking a
    space takes effect on the next turn of every existing chat.
    """
    access = agent["customer_access"]
    if access == "required" and not entity_id:
        raise ValueError(f"{agent['name']} works on one customer. Choose a customer to start this chat.")
    if access == "none" and entity_id:
        raise ValueError(f"{agent['name']} does not use customer documents.")
    sources: list[dict[str, Any]] = []
    entity: dict[str, Any] | None = None
    entity_document_ids: list[str] = []
    if entity_id:
        row = connection.execute("SELECT id, display_name FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError("Customer not found.")
        name = row["display_name"] or "Unnamed customer"
        entity = {"id": row["id"], "name": name}
        entity_document_ids = knowledge.entity_document_ids(connection, entity_id)
        sources.append({
            "kind": knowledge.ENTITY, "kindLabel": knowledge.KIND_LABELS[knowledge.ENTITY],
            "id": entity_id, "name": f"{name} Documents", "documentCount": len(entity_document_ids),
        })
    space_ids = json.loads(agent["space_ids"] or "[]")
    for space_id in space_ids:
        space = knowledge.get_space(connection, space_id)
        if space is not None:
            sources.append({**knowledge.space_payload(space), "kind": space["kind"]})
    shared_document_ids = knowledge.space_document_ids(connection, [source["id"] for source in sources if source["kind"] != knowledge.ENTITY])
    return {
        "agent": {"id": agent["id"], "name": agent["name"]},
        "entity": entity,
        "sources": sources,
        "entityDocumentIds": entity_document_ids,
        "documentIds": list(dict.fromkeys([*entity_document_ids, *shared_document_ids])),
    }


def is_consistency_question(question: str) -> bool:
    return bool(CONSISTENCY_TERMS.search(question))


def _normalise_value(row: sqlite3.Row) -> str:
    if row["normalized_value"] is not None and row["value_type"] in {"money", "number", "percentage"}:
        return f"{row['currency'] or ''}{row['normalized_value']}"
    value = str(row["raw_value"]).strip().lower()
    date = _normalise_date(value)
    if date:
        return date
    return re.sub(r"[^a-z0-9]", "", value)


def _normalise_date(value: str) -> str | None:
    from datetime import datetime as parser
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y"):
        try:
            return parser.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def consistency_check(connection: sqlite3.Connection, document_ids: list[str]) -> tuple[list[dict[str, Any]], int, list[sqlite3.Row]]:
    """Compare facts mapped to the same concept across different documents.

    Returns (conflicts, compared concept count, evidence rows). This is a
    deterministic comparison: no model decides what counts as a mismatch.
    """
    if len(document_ids) < 2:
        return [], 0, []
    placeholders = ",".join("?" for _ in document_ids)
    rows = connection.execute(
        f"""SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id
        WHERE facts.document_id IN ({placeholders}) AND facts.canonical_concept IS NOT NULL
        ORDER BY facts.canonical_concept, facts.document_id, facts.page_number, facts.id""",
        document_ids,
    ).fetchall()
    by_concept: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if not row["canonical_concept"].startswith(CONSISTENCY_EXCLUDED_PREFIXES):
            by_concept[row["canonical_concept"]].append(row)
    conflicts: list[dict[str, Any]] = []
    evidence: list[sqlite3.Row] = []
    compared = 0
    for concept, concept_rows in by_concept.items():
        if len({row["document_id"] for row in concept_rows}) < 2:
            continue
        compared += 1
        # One representative value per document and normalised value.
        values: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        for row in concept_rows:
            values[_normalise_value(row)].setdefault(row["document_id"], row)
        if len(values) < 2:
            continue
        variants = [next(iter(per_document.values())) for per_document in values.values()]
        conflicts.append({"concept": concept, "label": variants[0]["raw_label"], "variants": variants})
        evidence.extend(variants)
    return conflicts, compared, evidence


def describe_conflicts(conflicts: list[dict[str, Any]], compared: int, customer_name: str) -> str:
    if not conflicts:
        if compared == 0:
            return (f"I could not run a consistency check: none of {customer_name}'s facts appear in more than one document, "
                    "so there is nothing to compare yet.")
        return (f"No inconsistencies found. I compared {compared} fact type{'s' if compared != 1 else ''} that appear in more "
                f"than one of {customer_name}'s documents, and every document records the same value.")
    lines = [f"I found {len(conflicts)} fact{'s' if len(conflicts) != 1 else ''} recorded differently across {customer_name}'s documents:"]
    for conflict in conflicts:
        variants = "; ".join(f"\"{row['raw_value']}\" in {row['original_name']} (page {row['page_number']})" for row in conflict["variants"])
        lines.append(f"- {conflict['label']}: {variants}")
    lines.append(f"\nCompared {compared} fact type{'s' if compared != 1 else ''} found in more than one document. "
                 "Differences may be legitimate (for example, income from different months) and should be reviewed.")
    return "\n".join(lines)
