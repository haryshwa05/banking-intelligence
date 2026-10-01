import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class ScopeApiTests(unittest.TestCase):
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
            for document_id in ("peter-app", "peter-slip", "tony-slip"):
                connection.execute(
                    "INSERT INTO uploads(id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, 'application/pdf', 1, '2026-01-01')",
                    (document_id, f"{document_id}.pdf", f"{document_id}.pdf"),
                )
                connection.execute(
                    "INSERT INTO extractions(upload_id, status, created_at, updated_at) VALUES (?, 'completed', '2026-01-01', '2026-01-01')",
                    (document_id,),
                )
                connection.execute("INSERT INTO document_facts(upload_id, status, extraction_version) VALUES (?, 'ready', 'test')", (document_id,))
            connection.commit()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.uploads_patch.stop()
        self.temp.cleanup()

    def test_manual_assignment_and_selected_customer_scope(self) -> None:
        peter = self.client.post("/entities", json={"name": "Peter Parker"}).json()
        tony = self.client.post("/entities", json={"name": "Tony Stark"}).json()
        self.assertEqual(self.client.put("/uploads/peter-app/entity", json={"entityId": peter["id"]}).status_code, 200)
        self.client.put("/uploads/peter-slip/entity", json={"entityId": peter["id"]})
        self.client.put("/uploads/tony-slip/entity", json={"entityId": tony["id"]})
        self.assertEqual(next(item for item in self.client.get("/entities").json() if item["id"] == peter["id"])["documentCount"], 2)

        seen: list[list[str] | None] = []
        fact_scopes: list[list[str] | None] = []
        with patch.object(main.facts, "answer_with_trace", side_effect=lambda _connection, _question, ids, **_: (fact_scopes.append(ids) or None, {})), patch.object(
            main.rag, "retrieve_with_trace", side_effect=lambda _connection, _question, ids: (seen.append(ids) or [], {})
        ):
            customer_response = self.client.post("/questions", json={"question": "Where does Peter work?", "entityId": peter["id"]})
            document_response = self.client.post("/questions", json={"question": "Compare these", "documentIds": ["peter-app", "tony-slip"]})
        self.assertEqual(customer_response.status_code, 200)
        self.assertEqual(set(seen[0] or []), {"peter-app", "peter-slip"})
        self.assertEqual(set(fact_scopes[0] or []), {"peter-app", "peter-slip"})
        self.assertEqual(document_response.status_code, 200)
        self.assertEqual(seen[1], ["peter-app", "tony-slip"])
        self.assertEqual(fact_scopes[1], ["peter-app", "tony-slip"])

    def test_scope_validation_does_not_fall_back_to_global_search(self) -> None:
        self.assertEqual(self.client.post("/questions", json={"question": "test", "documentIds": []}).status_code, 400)
        self.assertEqual(self.client.post("/questions", json={"question": "test", "documentIds": ["missing"]}).status_code, 404)
        self.assertEqual(self.client.post("/questions", json={"question": "test", "entityId": "missing"}).status_code, 404)
        self.assertEqual(self.client.post("/questions", json={"question": "test", "entityId": "a", "documentIds": ["peter-app"]}).status_code, 400)

    def test_reassignment_removes_identifier_evidence_from_previous_customer(self) -> None:
        old = self.client.post("/entities", json={"name": "Original customer"}).json()
        new = self.client.post("/entities", json={"name": "Correct customer"}).json()
        self.client.put("/uploads/peter-app/entity", json={"entityId": old["id"]})
        with closing(main.get_connection()) as connection:
            connection.execute(
                "INSERT INTO entity_identifiers(id, entity_id, kind, normalized_value, display_value, source_document_id, created_at) VALUES ('i1', ?, 'customer_id', '123', '123', 'peter-app', '2026-01-01')",
                (old["id"],),
            )
            connection.commit()
        response = self.client.put("/uploads/peter-app/entity", json={"entityId": new["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entityId"], new["id"])
        with closing(main.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entity_identifiers WHERE source_document_id='peter-app'").fetchone()[0], 0)

    def test_manual_assignment_works_when_extraction_failed(self) -> None:
        customer = self.client.post("/entities", json={"name": "Customer with unreadable file"}).json()
        with closing(main.get_connection()) as connection:
            connection.execute("UPDATE extractions SET status='failed' WHERE upload_id='peter-app'")
            connection.execute("UPDATE document_facts SET status='failed' WHERE upload_id='peter-app'")
            connection.commit()
        response = self.client.put("/uploads/peter-app/entity", json={"entityId": customer["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entityId"], customer["id"])


if __name__ == "__main__":
    unittest.main()
