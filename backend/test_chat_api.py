import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class ChatApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database_patch = patch.object(main, "DATABASE_PATH", root / "metadata.db")
        self.uploads_patch = patch.object(main, "UPLOADS_DIR", root / "uploads")
        self.database_patch.start()
        self.uploads_patch.start()
        main.initialise_storage()
        self.client = TestClient(main.app)
        with closing(main.get_connection()) as connection:
            for doc_id in ("doc-one", "doc-two"):
                connection.execute(
                    "INSERT INTO uploads(id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, 'application/pdf', 1, '2026-01-01')",
                    (doc_id, f"{doc_id}.pdf", f"{doc_id}.pdf"),
                )
            connection.commit()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.uploads_patch.stop()
        self.temp.cleanup()

    def test_conversation_scope_and_history_survive_retrieval(self) -> None:
        created = self.client.post("/conversations", json={"scopeMode": "documents", "documentIds": ["doc-one"]})
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["id"]
        seen = []
        with patch.object(main.facts, "answer_with_trace", side_effect=lambda _db, question, ids, **_: (seen.append((question, ids)) or None, {})), patch.object(
            main.rag, "retrieve_with_trace", return_value=([], {})
        ), patch.object(main.chat_history, "_claude_text", return_value="What is the value in doc-one?"):
            first = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "What is the value?", "turnId": "turn-1"})
            second = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "What about it?", "turnId": "turn-2"})
        self.assertEqual(first.status_code, 200)
        self.assertIn("event: status", first.text)
        self.assertIn("event: final", first.text)
        self.assertIn("event: final", second.text)
        self.assertEqual([ids for _question, ids in seen], [["doc-one"], ["doc-one"]])
        self.assertEqual(seen[1][0], "What is the value in doc-one?")
        detail = self.client.get(f"/conversations/{conversation_id}").json()
        self.assertEqual(len(detail["messages"]), 4)
        self.assertEqual(detail["title"], "What is the value?")
        self.assertEqual(self.client.get("/conversations").json()[0]["id"], conversation_id)

    def test_stream_final_contains_only_validated_citations(self) -> None:
        conversation_id = self.client.post("/conversations", json={"scopeMode": "all"}).json()["id"]
        candidates = [
            {"id": "c1", "upload_id": "doc-one", "original_name": "doc-one.pdf", "page_number": 1},
            {"id": "c2", "upload_id": "doc-two", "original_name": "doc-two.pdf", "page_number": 2},
        ]
        with patch.object(main.facts, "answer_with_trace", return_value=(None, {})), patch.object(
            main.rag, "retrieve_with_trace", return_value=(candidates, {})
        ), patch.object(main.rag, "answer_stream", return_value=iter([
            {"type": "delta", "text": "The answer is 42."},
            {"type": "final", "answer": "The answer is 42.", "citedChunkIds": ["c1", "not-a-candidate"]},
        ])):
            response = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "What is it?", "turnId": "turn-1"})
        self.assertIn("event: delta", response.text)
        self.assertIn("event: final", response.text)
        detail = self.client.get(f"/conversations/{conversation_id}").json()
        assistant = detail["messages"][1]
        self.assertEqual([source["chunkId"] for source in assistant["sources"]], ["c1"])
        self.assertEqual(assistant["debug"]["claudeValidatedCitationIds"], ["c1"])

    def test_invalid_scope_and_turn_replay(self) -> None:
        self.assertEqual(self.client.post("/conversations", json={"scopeMode": "documents", "documentIds": ["missing"]}).status_code, 404)
        conversation_id = self.client.post("/conversations", json={"scopeMode": "documents", "documentIds": ["doc-one"]}).json()["id"]
        with patch.object(main.facts, "answer_with_trace", return_value=(("Verified answer", []), {})):
            first = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "Question", "turnId": "same-turn"})
        replay = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "Question", "turnId": "same-turn"})
        self.assertIn("event: final", first.text)
        self.assertIn("event: final", replay.text)
        self.assertEqual(len(self.client.get(f"/conversations/{conversation_id}").json()["messages"]), 2)
        self.assertEqual(self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "Different", "turnId": "same-turn"}).status_code, 409)

    def test_customer_chat_never_falls_back_to_all_documents(self) -> None:
        customer = self.client.post("/entities", json={"name": "Peter Parker"}).json()
        with closing(main.get_connection()) as connection:
            main.entities.assign_document(connection, "doc-one", customer["id"])
            connection.commit()
        conversation_id = self.client.post("/conversations", json={"scopeMode": "customer", "entityId": customer["id"]}).json()["id"]
        scopes = []
        with patch.object(main.facts, "answer_with_trace", side_effect=lambda _db, _question, ids, **_: (scopes.append(ids) or None, {})), patch.object(
            main.rag, "retrieve_with_trace", return_value=([], {})
        ):
            response = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "What is his salary?"})
        self.assertIn("event: final", response.text)
        self.assertEqual(scopes, [["doc-one"]])

    def test_deleted_scoped_document_fails_closed(self) -> None:
        conversation_id = self.client.post("/conversations", json={"scopeMode": "documents", "documentIds": ["doc-one"]}).json()["id"]
        with closing(main.get_connection()) as connection:
            connection.execute("DELETE FROM uploads WHERE id='doc-one'")
            connection.commit()
        response = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "What is in it?"})
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: final", response.text)
        detail = self.client.get(f"/conversations/{conversation_id}").json()
        self.assertEqual(detail["messages"][0]["status"], "failed")

    def test_rename_delete_and_pagination(self) -> None:
        first = self.client.post("/conversations", json={"scopeMode": "all"}).json()
        second = self.client.post("/conversations", json={"scopeMode": "all"}).json()
        renamed = self.client.patch(f"/conversations/{first['id']}", json={"title": "Income review"})
        self.assertEqual(renamed.json()["title"], "Income review")
        self.assertEqual(len(self.client.get("/conversations?limit=1&offset=0").json()), 1)
        self.assertEqual(len(self.client.get("/conversations?limit=1&offset=1").json()), 1)
        self.assertEqual(self.client.delete(f"/conversations/{second['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/conversations/{second['id']}").status_code, 404)

    def test_cancel_is_attempt_scoped_and_retryable(self) -> None:
        conversation_id = self.client.post("/conversations", json={"scopeMode": "all"}).json()["id"]
        with closing(main.get_connection()) as connection:
            connection.execute(
                "INSERT INTO chat_messages(id, conversation_id, turn_id, role, content, status, attempt_id, created_at) VALUES ('u1', ?, 'turn-1', 'user', 'Question', 'pending', 'old-attempt', '2026-01-01')",
                (conversation_id,),
            )
            connection.commit()
        wrong = self.client.post(f"/conversations/{conversation_id}/turns/turn-1/cancel", json={"attemptId": "other"})
        self.assertEqual(wrong.json()["status"], "unchanged")
        stopped = self.client.post(f"/conversations/{conversation_id}/turns/turn-1/cancel", json={"attemptId": "old-attempt"})
        self.assertEqual(stopped.json()["status"], "interrupted")
        with patch.object(main.facts, "answer_with_trace", return_value=(("Verified answer", []), {})):
            retry = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": "Question", "turnId": "turn-1", "attemptId": "new-attempt"})
        self.assertIn("event: final", retry.text)
        stale = self.client.post(f"/conversations/{conversation_id}/turns/turn-1/cancel", json={"attemptId": "old-attempt"})
        self.assertEqual(stale.json()["status"], "unchanged")
        self.assertEqual(self.client.get(f"/conversations/{conversation_id}").json()["messages"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
