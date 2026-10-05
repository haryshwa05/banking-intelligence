import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class AgentKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database_patch = patch.object(main, "DATABASE_PATH", root / "metadata.db")
        self.uploads_patch = patch.object(main, "UPLOADS_DIR", root / "uploads")
        self.database_patch.start()
        self.uploads_patch.start()
        main.initialise_storage()
        self.client = TestClient(main.app)
        documents = ("tony-app", "tony-slip", "bruce-slip", "loan-policy", "kyc-policy", "review-sop")
        with closing(main.get_connection()) as connection:
            for document_id in documents:
                connection.execute(
                    "INSERT INTO uploads(id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, 'application/pdf', 1, '2026-01-01')",
                    (document_id, f"{document_id}.pdf", f"{document_id}.pdf"),
                )
                connection.execute("INSERT INTO extractions(upload_id, status, created_at, updated_at) VALUES (?, 'completed', '2026-01-01', '2026-01-01')", (document_id,))
                connection.execute("INSERT INTO document_facts(upload_id, status, extraction_version) VALUES (?, 'ready', 'test')", (document_id,))
            connection.commit()
        self.tony = self.client.post("/entities", json={"name": "Tony Stark"}).json()
        self.bruce = self.client.post("/entities", json={"name": "Bruce Wayne"}).json()
        for document_id, customer in (("tony-app", self.tony), ("tony-slip", self.tony), ("bruce-slip", self.bruce)):
            self.assertEqual(self.client.put(f"/uploads/{document_id}/entity", json={"entityId": customer["id"]}).status_code, 200)
        for document_id, space_id in (("loan-policy", "lending-policies"), ("kyc-policy", "compliance-regulatory"), ("review-sop", "loan-operations")):
            self.assertEqual(self.client.put(f"/uploads/{document_id}/knowledge", json={"spaceId": space_id}).status_code, 200)

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.uploads_patch.stop()
        self.temp.cleanup()

    def _ask(self, conversation_id: str, question: str, turn_id: str = "turn-1"):
        scopes: list[tuple[list[str] | None, set[str] | None]] = []
        retrievals: list[list[str] | None] = []
        with patch.object(main.facts, "answer_with_trace", side_effect=lambda _db, _q, ids, capabilities=None: (scopes.append((ids, capabilities)) or None, {})), patch.object(
            main.rag, "retrieve_with_trace", side_effect=lambda _db, _q, ids: (retrievals.append(ids) or [], {})
        ), patch.object(main.chat_history, "_claude_text", return_value=question):
            response = self.client.post(f"/conversations/{conversation_id}/messages/stream", json={"question": question, "turnId": turn_id})
        self.assertEqual(response.status_code, 200)
        return response, scopes, retrievals

    def _chat(self, agent_id: str, entity_id: str | None) -> dict:
        payload = {"agentId": agent_id}
        if entity_id:
            payload["entityId"] = entity_id
        response = self.client.post("/conversations", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_documents_are_classified_into_visible_knowledge_spaces(self) -> None:
        documents = {item["id"]: item for item in self.client.get("/uploads").json()}
        self.assertEqual(documents["tony-app"]["knowledgeKind"], "entity")
        self.assertEqual(documents["tony-app"]["entityName"], "Tony Stark")
        self.assertEqual(documents["loan-policy"]["knowledgeKind"], "reference")
        self.assertEqual(documents["loan-policy"]["spaceName"], "Lending Policies")
        self.assertIsNone(documents["loan-policy"]["entityId"])
        self.assertEqual(documents["review-sop"]["knowledgeKind"], "operational")
        spaces = {item["id"]: item for item in self.client.get("/knowledge/spaces").json()}
        self.assertEqual(spaces["lending-policies"]["documentCount"], 1)
        # Shared knowledge cannot be given a customer owner.
        self.assertEqual(self.client.put("/uploads/loan-policy/entity", json={"entityId": self.tony["id"]}).status_code, 409)
        # A space still holding documents cannot be deleted.
        self.assertEqual(self.client.delete("/knowledge/spaces/lending-policies").status_code, 409)

    def test_filing_a_customer_document_as_policy_removes_its_customer_ownership(self) -> None:
        with closing(main.get_connection()) as connection:
            connection.execute(
                "INSERT INTO entity_identifiers(id, entity_id, kind, normalized_value, display_value, source_document_id, created_at) VALUES ('i1', ?, 'customer_id', '1', '1', 'tony-slip', '2026')",
                (self.tony["id"],),
            )
            connection.commit()
        moved = self.client.put("/uploads/tony-slip/knowledge", json={"spaceId": "lending-policies"}).json()
        self.assertEqual(moved["knowledgeKind"], "reference")
        self.assertIsNone(moved["entityId"])
        with closing(main.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entity_identifiers WHERE source_document_id='tony-slip'").fetchone()[0], 0)
        returned = self.client.put("/uploads/tony-slip/knowledge", json={"spaceId": None}).json()
        self.assertEqual(returned["knowledgeKind"], "entity")
        self.assertEqual(returned["entityStatus"], "queued")

    def test_same_agent_uses_each_chats_customer_and_its_granted_spaces_only(self) -> None:
        tony_chat = self._chat("loan-eligibility-analyst", self.tony["id"])
        bruce_chat = self._chat("loan-eligibility-analyst", self.bruce["id"])
        _, tony_scopes, tony_retrievals = self._ask(tony_chat["id"], "Is the requested amount within policy?")
        _, bruce_scopes, _ = self._ask(bruce_chat["id"], "Is the requested amount within policy?")
        self.assertEqual(set(tony_scopes[0][0]), {"tony-app", "tony-slip", "loan-policy"})
        self.assertEqual(set(tony_retrievals[0]), {"tony-app", "tony-slip", "loan-policy"})
        self.assertEqual(set(bruce_scopes[0][0]), {"bruce-slip", "loan-policy"})
        self.assertIn("policy_evaluation", tony_scopes[0][1])
        detail = self.client.get(f"/conversations/{tony_chat['id']}").json()
        self.assertEqual(detail["agentId"], "loan-eligibility-analyst")
        self.assertEqual([source["name"] for source in detail["knowledge"]["sources"]], ["Tony Stark Documents", "Lending Policies"])
        self.assertEqual(detail["messages"][1]["context"]["entity"]["name"], "Tony Stark")

    def test_agents_differ_in_knowledge_and_capabilities(self) -> None:
        analyst = self._chat("customer-document-analyst", self.tony["id"])
        compliance = self._chat("compliance-analyst", self.tony["id"])
        _, analyst_scopes, _ = self._ask(analyst["id"], "What is the salary?")
        _, compliance_scopes, _ = self._ask(compliance["id"], "What is the salary?")
        self.assertEqual(set(analyst_scopes[0][0]), {"tony-app", "tony-slip"})
        self.assertNotIn("policy_evaluation", analyst_scopes[0][1])
        self.assertEqual(set(compliance_scopes[0][0]), {"tony-app", "tony-slip", "kyc-policy"})
        self.assertNotIn("calculations", compliance_scopes[0][1])

    def test_compliance_agent_without_customer_uses_shared_knowledge_only(self) -> None:
        chat = self._chat("compliance-analyst", None)
        _, scopes, _ = self._ask(chat["id"], "What does the AML policy require?")
        self.assertEqual(scopes[0][0], ["kyc-policy"])

    def test_customer_required_agents_reject_chats_without_a_customer(self) -> None:
        response = self.client.post("/conversations", json={"agentId": "loan-eligibility-analyst"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Choose a customer", response.json()["detail"])

    def test_naming_another_customer_is_refused_without_retrieval(self) -> None:
        chat = self._chat("customer-document-analyst", self.tony["id"])
        response, scopes, retrievals = self._ask(chat["id"], "What is Bruce Wayne's salary?")
        self.assertIn("scoped to Tony Stark", response.text)
        self.assertEqual(scopes, [])
        self.assertEqual(retrievals, [])

    def test_empty_knowledge_never_falls_back_to_the_whole_library(self) -> None:
        empty = self.client.post("/entities", json={"name": "Peter Parker"}).json()
        chat = self._chat("customer-document-analyst", empty["id"])
        response, scopes, retrievals = self._ask(chat["id"], "Summarize this customer")
        self.assertIn("no documents", response.text)
        self.assertEqual((scopes, retrievals), ([], []))

    def test_consistency_check_flags_conflicting_values_across_documents(self) -> None:
        with closing(main.get_connection()) as connection:
            rows = [
                ("f1", "tony-app", "Date of Birth", "29/05/1970", "date", "person.date_of_birth"),
                ("f2", "tony-slip", "DOB", "1970-05-29", "date", "person.date_of_birth"),
                ("f3", "tony-app", "Employer", "Stark Industries", "text", "employment.employer.name"),
                ("f4", "tony-slip", "Company", "Stark Enterprises", "text", "employment.employer.name"),
                ("f5", "bruce-slip", "Company", "Wayne Enterprises", "text", "employment.employer.name"),
            ]
            for fact_id, document_id, label, value, value_type, concept in rows:
                connection.execute(
                    """INSERT INTO facts(id, document_id, page_number, fact_group_id, raw_label, raw_value, normalized_value, value_type,
                    canonical_concept, evidence_text, content_hash, extraction_version) VALUES (?, ?, 1, 'g', ?, ?, NULL, ?, ?, ?, 'h', 'v')""",
                    (fact_id, document_id, label, value, value_type, concept, f"{label}: {value}"),
                )
            connection.commit()
        chat = self._chat("customer-document-analyst", self.tony["id"])
        response, scopes, _ = self._ask(chat["id"], "Are there any inconsistencies across the documents?")
        self.assertIn("Stark Industries", response.text)
        self.assertIn("Stark Enterprises", response.text)
        self.assertNotIn("Wayne Enterprises", response.text)
        self.assertNotIn("Date of Birth", response.text)  # same date in two formats is consistent
        self.assertEqual(scopes, [])
        # The loan agent has no consistency capability and takes the normal route.
        loan_chat = self._chat("loan-eligibility-analyst", self.tony["id"])
        _, loan_scopes, _ = self._ask(loan_chat["id"], "Are there any inconsistencies across the documents?")
        self.assertEqual(len(loan_scopes), 1)

    def test_agent_model_and_instructions_reach_answer_generation(self) -> None:
        chat = self._chat("loan-eligibility-analyst", self.tony["id"])
        chunk = {"id": "c1", "upload_id": "tony-app", "original_name": "tony-app.pdf", "page_number": 1}
        calls = []

        def stream(_question, _chunks, **options):
            calls.append(options)
            yield {"type": "final", "answer": "Grounded", "citedChunkIds": ["c1"]}

        with patch.object(main.facts, "answer_with_trace", return_value=(None, {})), patch.object(
            main.rag, "retrieve_with_trace", return_value=([chunk], {})
        ), patch.object(main.rag, "answer_stream", side_effect=stream):
            response = self.client.post(f"/conversations/{chat['id']}/messages/stream", json={"question": "Eligible?", "turnId": "t"})
        self.assertIn("Grounded", response.text)
        self.assertEqual(calls[0]["model"], "claude-haiku-4-5-20251001")
        self.assertIn("loan eligibility analyst", calls[0]["instructions"])

    def test_agent_builder_validation_and_lifecycle(self) -> None:
        catalog = self.client.get("/agents/catalog").json()
        self.assertEqual([model["id"] for model in catalog["models"]], ["claude-haiku-4-5-20251001"])
        base = {
            "name": "Loan Review Assistant", "purpose": "Applies the review SOP", "description": "", "instructions": "Follow the SOP.",
            "model": "claude-haiku-4-5-20251001", "customerAccess": "required", "spaceIds": ["loan-operations"],
            "capabilities": ["document_search", "fact_lookup"],
        }
        self.assertEqual(self.client.post("/agents", json={**base, "model": "claude-opus-5-5"}).status_code, 400)
        self.assertEqual(self.client.post("/agents", json={**base, "customerAccess": "none", "spaceIds": []}).status_code, 400)
        self.assertEqual(self.client.post("/agents", json={**base, "capabilities": ["delete_everything"]}).status_code, 400)
        self.assertEqual(self.client.post("/agents", json={**base, "name": "Compliance Analyst"}).status_code, 400)
        created = self.client.post("/agents", json=base)
        self.assertEqual(created.status_code, 201)
        agent = created.json()
        preview = self.client.get(f"/agents/{agent['id']}/knowledge", params={"entityId": self.tony["id"]}).json()
        self.assertEqual([source["name"] for source in preview["sources"]], ["Tony Stark Documents", "Loan Review Procedures"])
        chat = self._chat(agent["id"], self.tony["id"])
        self.assertEqual([item["id"] for item in self.client.get("/conversations", params={"agentId": agent["id"]}).json()], [chat["id"]])
        self.assertEqual(self.client.delete(f"/agents/{agent['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/conversations/{chat['id']}").status_code, 404)

    def test_deleting_a_customer_unassigns_documents_and_removes_their_chats(self) -> None:
        tony_chat = self._chat("customer-document-analyst", self.tony["id"])
        bruce_chat = self._chat("customer-document-analyst", self.bruce["id"])
        renamed = self.client.patch(f"/entities/{self.tony['id']}", json={"name": "Anthony Stark"})
        self.assertEqual(renamed.json()["name"], "Anthony Stark")
        result = self.client.delete(f"/entities/{self.tony['id']}").json()
        self.assertEqual(result, {"unassignedDocuments": 2, "deletedChats": 1})
        documents = {item["id"]: item for item in self.client.get("/uploads").json()}
        self.assertIsNone(documents["tony-app"]["entityId"])
        self.assertEqual(documents["tony-app"]["entityStatus"], "needs_review")
        self.assertEqual(self.client.get(f"/conversations/{tony_chat['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/conversations/{bruce_chat['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/entities/{self.tony['id']}").status_code, 404)

    def test_document_can_be_removed_from_its_customer(self) -> None:
        response = self.client.delete("/uploads/tony-slip/entity")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["entityId"])
        chat = self._chat("customer-document-analyst", self.tony["id"])
        _, scopes, _ = self._ask(chat["id"], "What is the salary?")
        self.assertEqual(scopes[0][0], ["tony-app"])

    def test_space_can_be_deleted_by_moving_documents_back_to_customer_knowledge(self) -> None:
        self.assertEqual(self.client.delete("/knowledge/spaces/lending-policies").status_code, 409)
        self.assertEqual(self.client.delete("/knowledge/spaces/lending-policies", params={"moveDocuments": "true"}).status_code, 204)
        document = next(item for item in self.client.get("/uploads").json() if item["id"] == "loan-policy")
        self.assertEqual(document["knowledgeKind"], "entity")
        loan = self.client.get("/agents/loan-eligibility-analyst").json()
        self.assertEqual(loan["spaceIds"], [])

    def test_all_chats_of_an_agent_can_be_deleted_and_built_in_agents_deleted(self) -> None:
        self._chat("loan-eligibility-analyst", self.tony["id"])
        self._chat("loan-eligibility-analyst", self.bruce["id"])
        keep = self._chat("customer-document-analyst", self.tony["id"])
        self.assertEqual(self.client.delete("/conversations", params={"agentId": "loan-eligibility-analyst"}).json(), {"deletedChats": 2})
        self.assertEqual([item["id"] for item in self.client.get("/conversations").json()], [keep["id"]])
        self.assertEqual(self.client.delete("/agents/compliance-analyst").status_code, 204)
        self.assertNotIn("compliance-analyst", [item["id"] for item in self.client.get("/agents").json()])

    def test_upload_can_be_filed_directly_into_a_shared_space(self) -> None:
        response = self.client.post("/uploads", files={"file": ("aml.txt", b"AML policy", "text/plain")}, data={"spaceId": "compliance-regulatory"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["spaceName"], "Compliance & Regulatory")
        missing = self.client.post("/uploads", files={"file": ("x.txt", b"x", "text/plain")}, data={"spaceId": "missing"})
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
