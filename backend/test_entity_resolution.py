import sqlite3
import unittest

from entity_resolution import EntityResolver


class EntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE uploads (id TEXT PRIMARY KEY)")
        self.connection.execute("CREATE TABLE facts (id TEXT, document_id TEXT, raw_label TEXT, raw_value TEXT, evidence_text TEXT, page_number INTEGER)")
        identities = {
            "first": {"displayName": "Tony Stark", "identifiers": [{"kind": "customer_id", "value": "CUST-48291"}, {"kind": "full_name", "value": "Tony Stark"}]},
            "later": {"displayName": "Tony Stark", "identifiers": [{"kind": "customer_id", "value": "CUST-48291"}, {"kind": "account_number", "value": "1234 5678"}]},
            "name-only": {"displayName": "Tony Stark", "identifiers": [{"kind": "full_name", "value": "Tony Stark"}]},
        }
        self.resolver = EntityResolver(lambda facts: identities[facts[0]["raw_value"]])
        self.resolver.initialise(self.connection)

    def add_document(self, document_id: str, marker: str) -> None:
        self.connection.execute("INSERT INTO uploads VALUES (?)", (document_id,))
        self.connection.execute("INSERT INTO facts VALUES (?, ?, 'Marker', ?, 'Marker', 1)", (f"{document_id}:0", document_id, marker))

    def test_later_document_links_to_existing_customer_by_identifier(self):
        self.add_document("application", "first")
        self.add_document("statement", "later")
        self.resolver.resolve_document(self.connection, "application")
        self.resolver.resolve_document(self.connection, "statement")

        links = self.connection.execute("SELECT document_id, entity_id, status FROM document_entity_links ORDER BY document_id").fetchall()
        self.assertEqual([link["status"] for link in links], ["linked", "linked"])
        self.assertEqual(links[0]["entity_id"], links[1]["entity_id"])

    def test_name_only_document_requires_review(self):
        self.add_document("application", "first")
        self.add_document("unknown", "name-only")
        self.resolver.resolve_document(self.connection, "application")
        self.resolver.resolve_document(self.connection, "unknown")

        link = self.connection.execute("SELECT entity_id, status FROM document_entity_links WHERE document_id='unknown'").fetchone()
        self.assertEqual(link["status"], "needs_review")
        self.assertIsNone(link["entity_id"])

    def test_question_scope_uses_only_the_named_customers_linked_documents(self):
        self.add_document("application", "first")
        self.add_document("statement", "later")
        self.resolver.resolve_document(self.connection, "application")
        self.resolver.resolve_document(self.connection, "statement")

        scope = self.resolver.question_scope(self.connection, "Where does Tony Stark work?")

        self.assertEqual(scope["mode"], "entity")
        self.assertEqual(scope["entityName"], "Tony Stark")
        self.assertEqual(set(scope["documentIds"]), {"application", "statement"})


if __name__ == "__main__":
    unittest.main()
