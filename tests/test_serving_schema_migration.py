"""Static, offline guardrails for the incident serving migration."""
from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "db" / "009_incident_serving.sql"
SQL = MIGRATION.read_text(encoding="utf-8")


class IncidentServingMigrationTests(unittest.TestCase):
    def test_creates_four_serving_tables_with_independent_incident_uuid(self):
        for table in (
            "incidents",
            "incident_evidence",
            "incident_reviews",
            "incident_verifications",
        ):
            self.assertRegex(SQL, rf"CREATE TABLE serving\.{table} \(")
        self.assertRegex(SQL, r"id uuid PRIMARY KEY DEFAULT gen_random_uuid\(\)")
        self.assertIn("CREATE SCHEMA serving;", SQL)

    def test_evidence_links_are_explicit_and_pin_matching_event_review(self):
        self.assertIn("REFERENCES processing.events(id) ON DELETE RESTRICT", SQL)
        self.assertRegex(
            SQL,
            r"FOREIGN KEY \(processing_event_id, event_review_revision\)\s+"
            r"REFERENCES processing.event_reviews\(event_id, revision\) ON DELETE RESTRICT",
        )
        self.assertIn("UNIQUE (incident_id, processing_event_id, event_review_revision)", SQL)
        self.assertIn("WHERE link_status = 'accepted'", SQL)

    def test_primary_evidence_and_publication_are_guarded(self):
        self.assertIn("WHERE is_primary;", SQL)
        self.assertIn("CREATE CONSTRAINT TRIGGER incidents_publication_requirements", SQL)
        self.assertIn("latest accepted human review", SQL)
        self.assertIn("accepted primary evidence", SQL)
        self.assertIn("ir.reviewed_title = current_title", SQL)
        self.assertIn("ir.reviewed_category = current_category", SQL)
        self.assertIn("per.review_status IN ('accepted','corrected')", SQL)
        self.assertIn("CREATE FUNCTION serving.assert_incident_evidence_reviewed", SQL)
        self.assertIn("per.review_status NOT IN ('accepted','corrected')", SQL)
        self.assertIn("CHECK (link_status <> 'accepted' OR", SQL)

    def test_lifecycle_and_geography_constraints_are_separate_and_validated(self):
        for state in ("draft", "approved", "withdrawn", "unknown", "active", "resolved"):
            self.assertIn(f"'{state}'", SQL)
        self.assertIn("CHECK ((latitude IS NULL) = (longitude IS NULL))", SQL)
        self.assertIn("latitude BETWEEN -90 AND 90", SQL)
        self.assertIn("longitude BETWEEN -180 AND 180", SQL)
        self.assertIn("event_end_at >= event_start_at", SQL)
        self.assertIn("location_station_id text REFERENCES reference.krl_stations", SQL)
        self.assertIn("station mention alone does not establish incident location", SQL)
        self.assertNotIn("nearest_station_id text", SQL.split("CREATE TABLE serving.incidents", 1)[1].split("CREATE TABLE serving.incident_evidence", 1)[0])

    def test_audit_history_is_append_only_and_foreign_keys_restrict_deletion(self):
        self.assertIn("CREATE TRIGGER incident_reviews_append_only", SQL)
        self.assertIn("CREATE TRIGGER incident_verifications_append_only", SQL)
        self.assertIn("incident audit records are append-only", SQL)
        self.assertNotRegex(SQL, r"ON DELETE CASCADE")
        self.assertGreaterEqual(SQL.count("ON DELETE RESTRICT"), 6)

    def test_private_permissions_and_gateway_projection_are_narrow(self):
        self.assertIn("REVOKE ALL ON SCHEMA serving FROM PUBLIC, anon, authenticated", SQL)
        self.assertIn("REVOKE ALL ON serving.incidents", SQL)
        self.assertIn("GRANT SELECT ON serving.gateway_corridor_incidents TO moobby_incident_gateway", SQL)
        view_sql = SQL.split("CREATE VIEW serving.gateway_corridor_incidents", 1)[1].split(";", 1)[0]
        self.assertIn('AS "locationName"', view_sql)
        self.assertIn('AS "nearestStationName"', view_sql)
        self.assertIn('AS "verificationStatus"', view_sql)
        self.assertNotRegex(view_sql, r"raw_text|evidence_quotes|evidence_summary|post_text")

    def test_internal_enums_are_not_claimed_as_client_enum_contract(self):
        self.assertIn("gateway adapter must map these explicitly to the actual app enum", SQL)
        self.assertIn("category and verificationStatus use internal serving values", SQL)
        self.assertIn("COALESCE(verification.verification_status, 'unverified')", SQL)
        self.assertIn("'corroborated','disputed','inconclusive'", SQL)

    def test_projection_has_all_contract_fields_and_uses_one_primary_snapshot(self):
        view_sql = SQL.split("CREATE VIEW serving.gateway_corridor_incidents", 1)[1].split(";", 1)[0]
        for field in (
            'AS id', 'AS title', 'AS category', 'AS "locationName"',
            'AS latitude', 'AS longitude', 'AS "nearestStationId"',
            'AS "nearestStationName"', 'AS "distanceToStationKm"',
            'AS "publishedAt"', 'AS "sourceName"', 'AS "verificationStatus"',
        ):
            self.assertIn(field, view_sql)
        self.assertIn("JOIN collection.raw_snapshots AS snapshot ON snapshot.id = event.snapshot_id", view_sql)
        self.assertIn("snapshot.published_at AS \"publishedAt\"", view_sql)
        self.assertIn("snapshot.author_display_name, snapshot.author_handle", view_sql)
        self.assertIn("current_review.reviewed_title = i.title", view_sql)
        self.assertIn("current_review.reviewed_category = i.category", view_sql)
        self.assertIn("6371.0088", view_sql)
        self.assertIn("ORDER BY candidate.distance_km, candidate.station_id", view_sql)
        self.assertNotIn("i.nearest_station_id", view_sql)
        self.assertIn("REVOKE ALL ON FUNCTION serving.assert_publishable_incident(uuid)", SQL)
        self.assertIn("REVOKE ALL ON FUNCTION serving.check_publication_after_change()", SQL)


if __name__ == "__main__":
    unittest.main()
