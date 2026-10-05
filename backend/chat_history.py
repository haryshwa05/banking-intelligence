"""Local conversation persistence and bounded follow-up context.

Conversation text is useful for interpreting a follow-up, but is never passed to
the document retrievers as evidence. Every answer still retrieves fresh facts or
chunks from the conversation's document scope.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


RECENT_TURNS = 6
SUMMARY_LIMIT = 1200


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialise(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            scope_mode TEXT NOT NULL, entity_id TEXT, document_ids TEXT,
            summary TEXT NOT NULL DEFAULT '', summary_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            attempt_id TEXT,
            sources_json TEXT NOT NULL DEFAULT '[]', mode TEXT, debug_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            UNIQUE (conversation_id, turn_id, role)
        )
    """)
    message_columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_messages)")}
    if "attempt_id" not in message_columns:
        connection.execute("ALTER TABLE chat_messages ADD COLUMN attempt_id TEXT")
    # The knowledge an answer was produced from, recorded at answer time so the
    # chat keeps an honest record even if the agent's access changes later.
    if "context_json" not in message_columns:
        connection.execute("ALTER TABLE chat_messages ADD COLUMN context_json TEXT")
    # Structured results shown with an answer, such as a spreadsheet result table.
    if "table_json" not in message_columns:
        connection.execute("ALTER TABLE chat_messages ADD COLUMN table_json TEXT")
    # Agent chats bind one reusable agent to (optionally) one customer.
    if "agent_id" not in {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}:
        connection.execute("ALTER TABLE conversations ADD COLUMN agent_id TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS conversations_agent ON conversations(agent_id, updated_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS chat_messages_conversation ON chat_messages(conversation_id, created_at, id)")
    # A restarted server may have interrupted a response mid-stream. The user
    # message remains available for retry, while no partial assistant answer is
    # treated as a completed answer.
    connection.execute("UPDATE chat_messages SET status='interrupted' WHERE status='pending'")


def conversation_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "scopeMode": row["scope_mode"],
        "entityId": row["entity_id"], "documentIds": json.loads(row["document_ids"] or "[]"),
        "agentId": row["agent_id"] if "agent_id" in row.keys() else None,
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def message_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "turnId": row["turn_id"], "role": row["role"],
        "content": row["content"], "status": row["status"],
        "sources": json.loads(row["sources_json"] or "[]"), "mode": row["mode"],
        "debug": json.loads(row["debug_json"]) if row["debug_json"] else None,
        "context": json.loads(row["context_json"]) if "context_json" in row.keys() and row["context_json"] else None,
        "table": json.loads(row["table_json"]) if "table_json" in row.keys() and row["table_json"] else None,
        "createdAt": row["created_at"],
    }


def completed_turns(connection: sqlite3.Connection, conversation_id: str) -> list[tuple[str, str]]:
    rows = connection.execute("""
        SELECT u.content AS question, a.content AS answer
        FROM chat_messages AS u JOIN chat_messages AS a
          ON a.conversation_id=u.conversation_id AND a.turn_id=u.turn_id
        WHERE u.conversation_id=? AND u.role='user' AND a.role='assistant'
          AND a.status='completed'
        ORDER BY u.created_at, u.id
    """, (conversation_id,)).fetchall()
    return [(row["question"], row["answer"]) for row in rows]


def _claude_text(system: str, user: str, max_tokens: int) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the backend.")
    from anthropic import Anthropic

    message = Anthropic(api_key=key).messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in message.content if block.type == "text").strip()


def context_and_question(connection: sqlite3.Connection, conversation: sqlite3.Row, question: str) -> tuple[str, dict[str, Any]]:
    """Resolve references with bounded chat context, not document evidence."""
    turns = completed_turns(connection, conversation["id"])
    if not turns:
        return question, {"usedHistory": False, "recentTurns": 0, "summaryUsed": False}

    summary = conversation["summary"] or ""
    summary_count = int(conversation["summary_count"])
    target_count = max(0, len(turns) - RECENT_TURNS)
    # Compact in batches, not on every message after turn six. Until the next
    # batch, a slightly wider recent window retains the unsummarized turns.
    if target_count - summary_count >= RECENT_TURNS:
        older = turns[summary_count:target_count]
        transcript = "\n".join(f"User: {q[:500]}\nAssistant: {a[:700]}" for q, a in older)
        try:
            summary = _claude_text(
                "Summarize conversation context only: named subjects, questions, and conclusions. "
                "Do not treat prior answers as verified document evidence. Be concise.",
                f"Previous summary:\n{summary[:SUMMARY_LIMIT]}\n\nAdditional turns:\n{transcript[:10000]}", 350,
            )[:SUMMARY_LIMIT]
            connection.execute(
                "UPDATE conversations SET summary=?, summary_count=? WHERE id=?",
                (summary, target_count, conversation["id"]),
            )
            connection.commit()
        except Exception:
            # If compaction is unavailable, preserve the latest turns without
            # silently turning old answers into retrieval evidence.
            pass

    recent = turns[max(summary_count, len(turns) - (RECENT_TURNS * 2 - 1)):]
    transcript = "\n".join(f"User: {q[:500]}\nAssistant: {a[:700]}" for q, a in recent)
    prompt = (
        f"Earlier context summary:\n{summary[:SUMMARY_LIMIT]}\n\n"
        f"Recent turns:\n{transcript[:7000]}\n\nCurrent question:\n{question}"
    )
    try:
        rewritten = _claude_text(
            "Rewrite the current question as a standalone search question. Resolve pronouns and ellipsis "
            "using conversation context. Preserve any customer or document explicitly named in the current "
            "question over earlier context. Do not answer, add facts, or mention unsupported details. "
            "Return only the standalone question.",
            prompt, 180,
        ).strip().strip('"')
        if not rewritten or len(rewritten) > 1000:
            raise ValueError("Invalid standalone question")
    except Exception:
        rewritten = question
    return rewritten, {
        "usedHistory": True, "recentTurns": len(recent), "summaryUsed": bool(summary),
        "standaloneQuestion": rewritten,
    }


def new_id() -> str:
    return str(uuid.uuid4())
