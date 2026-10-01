import sqlite3
import unittest
from decimal import Decimal

from facts import FactEngine


class FormulaFactTests(unittest.TestCase):
    def _facts(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE facts (id TEXT, raw_label TEXT, raw_value TEXT, normalized_value TEXT, currency TEXT, subject TEXT, evidence_text TEXT)")
        connection.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("requested", "Amount requested", "INR 800000", "800000", "INR", None, "Amount requested: INR 800000"),
                ("rule", "Maximum amount", "12 times gross monthly income", None, None, None, "Maximum amount: 12 times gross monthly income"),
                ("income", "Gross monthly income", "INR 82000", "82000", "INR", None, "Gross monthly income: INR 82000"),
            ],
        )
        return connection.execute("SELECT * FROM facts ORDER BY id").fetchall()

    def test_multiplier_policy_is_evaluated_without_business_label_rules(self):
        facts = self._facts()
        answer, sources = FactEngine._formula_compare(
            facts,
            {
                "targetFactId": "requested",
                "formulaFactId": "rule",
                "baseFactId": "income",
                "comparison": "less_than_or_equal",
            },
        )
        self.assertIn("within", answer)
        self.assertIn("984000", answer)
        self.assertEqual([source["id"] for source in sources], ["requested", "rule", "income"])

    def test_currency_mismatch_is_not_calculated(self):
        facts = self._facts()
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE facts (id TEXT, raw_label TEXT, raw_value TEXT, normalized_value TEXT, currency TEXT, subject TEXT, evidence_text TEXT)")
        connection.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)", [tuple(row) for row in facts])
        connection.execute("UPDATE facts SET currency='USD' WHERE id='income'")
        altered = connection.execute("SELECT * FROM facts ORDER BY id").fetchall()
        answer, _ = FactEngine._formula_compare(altered, {"targetFactId": "requested", "formulaFactId": "rule", "baseFactId": "income"})
        self.assertIn("different currencies", answer)

    def test_legacy_selector_response_can_infer_unambiguous_formula_roles(self):
        facts = self._facts()
        answer, sources = FactEngine._formula_compare(
            facts,
            {"factIds": ["requested", "rule", "income"], "comparison": "less_than_or_equal"},
        )
        self.assertIn("within", answer)
        self.assertEqual([source["id"] for source in sources], ["requested", "rule", "income"])

    def test_limit_question_uses_explicit_formula_and_matching_numeric_fact(self):
        plan = FactEngine._deterministic_formula_plan(
            "is the loan amount tony asked for withing his eligibility?",
            self._facts(),
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["targetFactId"], "requested")
        self.assertEqual(plan["formulaFactId"], "rule")
        self.assertEqual(plan["baseFactId"], "income")

    def test_no_formula_plan_for_a_non_limit_question(self):
        plan = FactEngine._deterministic_formula_plan("What is the amount requested?", self._facts())
        self.assertIsNone(plan)

    def test_natural_language_validation_requires_every_calculation_number(self):
        self.assertTrue(FactEngine._has_required_numbers("The request of INR 800,000 is within the INR 984,000 limit based on INR 82,000 income.", [Decimal("800000"), Decimal("82000"), Decimal("984000")]))
        self.assertFalse(FactEngine._has_required_numbers("The request is within the calculated limit.", [Decimal("800000"), Decimal("82000"), Decimal("984000")]))

    def test_natural_language_validation_rejects_a_contradictory_verdict(self):
        values = [Decimal("800000"), Decimal("82000"), Decimal("984000")]
        self.assertTrue(FactEngine._is_valid_formula_narration("Yes. INR 800,000 is within the INR 984,000 limit calculated from INR 82,000.", True, values))
        self.assertFalse(FactEngine._is_valid_formula_narration("No, the request is not within the limit, even though INR 800,000 is below INR 984,000 based on INR 82,000.", True, values))

    def test_fact_narration_validation_requires_the_verified_value(self):
        self.assertTrue(FactEngine._contains_fact_value("Tony's employer is Stark Industries Pvt Ltd.", "Stark Industries Pvt Ltd"))
        self.assertFalse(FactEngine._contains_fact_value("Tony works for Stark Industries.", "Stark Industries Pvt Ltd"))

    def test_profile_request_is_detected_without_a_document_field_list(self):
        self.assertTrue(FactEngine._is_profile_request("Tell me about customer Tony"))
        self.assertTrue(FactEngine._is_profile_request("Give me an overview of Tony"))
        self.assertFalse(FactEngine._is_profile_request("What is Tony's identity number?"))

    def test_profile_expansion_links_only_documents_containing_the_entity(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE uploads (id TEXT, original_name TEXT)")
        connection.execute("CREATE TABLE facts (id TEXT, document_id TEXT, page_number INTEGER, raw_label TEXT, raw_value TEXT)")
        connection.executemany("INSERT INTO uploads VALUES (?, ?)", [("application", "application.pdf"), ("employment", "employment.pdf"), ("other", "other.pdf")])
        connection.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?)",
            [
                ("a-name", "application", 1, "Applicant name", "Tony Stark"),
                ("a-income", "application", 1, "Monthly income", "82000"),
                ("a-loan", "application", 2, "Requested amount", "800000"),
                ("e-name", "employment", 1, "Employee name", "Tony Stark"),
                ("e-role", "employment", 1, "Role", "Engineer"),
                ("o-name", "other", 1, "Applicant name", "Pepper Potts"),
            ],
        )
        seeds = connection.execute("SELECT facts.*, uploads.original_name FROM facts JOIN uploads ON uploads.id=facts.document_id WHERE facts.id='a-name'").fetchall()
        engine = FactEngine(lambda texts: [], lambda question, texts: [])
        rows, trace = engine._profile_candidates(connection, "Tell me about customer Tony", seeds, None)
        self.assertEqual({row["document_id"] for row in rows}, {"application", "employment"})
        self.assertEqual({entry["filename"] for entry in trace["documents"]}, {"application.pdf", "employment.pdf"})


if __name__ == "__main__":
    unittest.main()
