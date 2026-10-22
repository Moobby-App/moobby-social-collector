"""Offline regression checks for the forward-only serving trigger fix."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SQL = (ROOT / "db" / "009_incident_serving.sql").read_text(encoding="utf-8")
FIX_SQL = (ROOT / "db" / "011_fix_serving_publication_trigger.sql").read_text(encoding="utf-8")


def function_body(sql: str) -> str:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION serving\.check_publication_after_change\(\).*?AS \$\$(.*?)\$\$;",
        sql,
        re.DOTALL,
    )
    if not match:
        # Migration 009 declares the original function with CREATE FUNCTION.
        match = re.search(
            r"CREATE FUNCTION serving\.check_publication_after_change\(\).*?AS \$\$(.*?)\$\$;",
            sql,
            re.DOTALL,
        )
    if not match:
        raise AssertionError("publication trigger function definition not found")
    return match.group(1)


class ServingPublicationTriggerFixTests(unittest.TestCase):
    def test_forward_migration_replaces_only_the_trigger_function(self):
        self.assertIn("BEGIN;", FIX_SQL)
        self.assertIn("COMMIT;", FIX_SQL)
        self.assertIn("CREATE OR REPLACE FUNCTION serving.check_publication_after_change()", FIX_SQL)
        self.assertNotRegex(FIX_SQL, r"\b(DROP|CREATE)\s+(TABLE|TRIGGER|POLICY)\b")
        self.assertIn("SECURITY DEFINER", FIX_SQL)
        self.assertIn("SET search_path = pg_catalog, serving, processing", FIX_SQL)
        self.assertIn("serving.assert_incident_evidence_reviewed(target_incident_id)", FIX_SQL)
        self.assertIn("serving.assert_publishable_incident(target_incident_id)", FIX_SQL)

    def test_draft_incident_insert_and_update_use_incident_id_column(self):
        body = function_body(FIX_SQL)
        incident_branch = body.split("IF TG_TABLE_NAME = 'incidents' THEN", 1)[1].split(
            "ELSIF TG_TABLE_NAME = 'incident_evidence' THEN", 1
        )[0]
        self.assertIn("TG_OP = 'DELETE'", incident_branch)
        self.assertIn("target_incident_id := OLD.id", incident_branch)
        self.assertIn("TG_OP IN ('INSERT', 'UPDATE')", incident_branch)
        self.assertIn("target_incident_id := NEW.id", incident_branch)
        self.assertNotRegex(incident_branch, r"\b(?:OLD|NEW)\.incident_id\b")
        self.assertRegex(incident_branch, r"target_incident_id := NEW\.id")

    def test_evidence_insert_update_delete_dispatch_and_reparent_both_sides(self):
        body = function_body(FIX_SQL)
        evidence_branch = body.split("ELSIF TG_TABLE_NAME = 'incident_evidence' THEN", 1)[1].split(
            "ELSIF TG_TABLE_NAME = 'incident_reviews' THEN", 1
        )[0]
        self.assertIn("IF TG_OP = 'UPDATE' AND OLD.incident_id IS DISTINCT FROM NEW.incident_id", evidence_branch)
        self.assertIn("assert_incident_evidence_reviewed(OLD.incident_id)", evidence_branch)
        self.assertIn("assert_publishable_incident(OLD.incident_id)", evidence_branch)
        self.assertIn("IF TG_OP = 'DELETE' THEN", evidence_branch)
        self.assertIn("target_incident_id := OLD.incident_id", evidence_branch)
        self.assertIn("TG_OP IN ('INSERT', 'UPDATE')", evidence_branch)
        self.assertIn("target_incident_id := NEW.incident_id", evidence_branch)

    def test_review_insert_uses_child_key_and_rejects_unwired_operations(self):
        body = function_body(FIX_SQL)
        review_branch = body.split("ELSIF TG_TABLE_NAME = 'incident_reviews' THEN", 1)[1].split(
            "ELSE\n    RAISE EXCEPTION 'unsupported table", 1
        )[0]
        self.assertIn("IF TG_OP <> 'INSERT' THEN", review_branch)
        self.assertIn("target_incident_id := NEW.incident_id", review_branch)

    def test_original_deferred_trigger_coverage_is_preserved(self):
        self.assertRegex(
            ORIGINAL_SQL,
            r"CREATE CONSTRAINT TRIGGER incidents_publication_requirements\s+"
            r"AFTER INSERT OR UPDATE ON serving\.incidents\s+"
            r"DEFERRABLE INITIALLY DEFERRED",
        )
        self.assertRegex(
            ORIGINAL_SQL,
            r"CREATE CONSTRAINT TRIGGER evidence_publication_requirements\s+"
            r"AFTER INSERT OR UPDATE OR DELETE ON serving\.incident_evidence\s+"
            r"DEFERRABLE INITIALLY DEFERRED",
        )
        self.assertRegex(
            ORIGINAL_SQL,
            r"CREATE CONSTRAINT TRIGGER reviews_publication_requirements\s+"
            r"AFTER INSERT ON serving\.incident_reviews\s+"
            r"DEFERRABLE INITIALLY DEFERRED",
        )


if __name__ == "__main__":
    unittest.main()
