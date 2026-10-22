from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'db/010_incident_curation_readonly.sql'
SQL = MIGRATION.read_text(encoding='utf-8')
CURATION_SOURCE = (ROOT / 'scripts/curate_incidents.py').read_text(encoding='utf-8')
RESOLVER_SOURCE = (ROOT / 'scripts/krl_station_resolver.py').read_text(encoding='utf-8')


REQUIRED_COLUMNS = {
    'collection.raw_snapshots': {
        'id', 'raw_content_id', 'published_at', 'author_handle', 'author_display_name',
    },
    'collection.raw_contents': {'id', 'canonical_url'},
    'processing.events': {
        'id', 'snapshot_id', 'event_index', 'event_status', 'disruption_type',
        'severity', 'affected_entities', 'event_start_time', 'event_end_time',
        'uncertainty_status', 'uncertainty_note', 'explanation', 'evidence_quotes',
    },
    'processing.event_reviews': {'event_id', 'revision', 'review_status'},
    'reference.krl_stations': {
        'station_id', 'source_station_code', 'canonical_name', 'aliases', 'latitude', 'longitude',
    },
    'serving.incident_evidence': {
        'id', 'processing_event_id', 'incident_id', 'link_status', 'relationship',
        'is_primary', 'created_at',
    },
}


class IncidentReadonlyMigrationTests(unittest.TestCase):
    def test_creates_dedicated_roles_without_embedded_password(self):
        self.assertRegex(SQL, r'CREATE ROLE moobby_incident_readonly NOLOGIN NOINHERIT')
        self.assertRegex(SQL, r'CREATE ROLE moobby_incident_readonly_login LOGIN INHERIT')
        self.assertIn('GRANT moobby_incident_readonly TO moobby_incident_readonly_login', SQL)
        self.assertNotRegex(SQL, r'CREATE ROLE[^;]*\bPASSWORD\b')
        self.assertIn('unexpected role memberships', SQL)

    def test_role_membership_guard_allows_only_privileged_admin_extra_grantees(self):
        self.assertRegex(
            SQL,
            r"SELECT 1 FROM pg_auth_members\s+WHERE member = read_role\s+\) THEN\s+"
            r"RAISE EXCEPTION 'moobby_incident_readonly must not inherit another role'",
        )
        self.assertRegex(
            SQL,
            r"JOIN pg_roles AS grantee ON grantee\.oid = membership\.member\s+"
            r"WHERE membership\.roleid = read_role\s+"
            r"AND membership\.member <> login_role\s+"
            r"AND NOT \(grantee\.rolsuper OR grantee\.rolcreaterole\)",
        )
        self.assertIn(
            "moobby_incident_readonly has an unexpected non-admin member", SQL
        )
        self.assertIn("WHERE member = login_role AND roleid <> read_role", SQL)

    def test_role_membership_guard_does_not_allow_extra_application_roles(self):
        # The exception is limited to database administrators. Ordinary roles
        # remain rejected, and the readonly role cannot inherit a parent role.
        guard = SQL.split("-- Other ordinary roles must not gain access", 1)[1].split(
            "END $$;", 1
        )[0]
        self.assertIn("AND NOT (grantee.rolsuper OR grantee.rolcreaterole)", guard)
        self.assertIn("WHERE member = read_role", SQL)
        self.assertNotIn("rolbypassrls", guard)

    def test_grants_only_required_columns_and_schemas(self):
        grant_columns = {}
        for match in re.finditer(
            r'GRANT SELECT\s*\((.*?)\)\s*ON\s+([\w.]+)\s+TO moobby_incident_readonly;',
            SQL, re.S,
        ):
            columns = {value.strip() for value in match.group(1).replace('\n', ' ').split(',')}
            grant_columns[match.group(2)] = columns
        self.assertEqual(grant_columns, REQUIRED_COLUMNS)
        for relation in REQUIRED_COLUMNS:
            self.assertNotRegex(SQL, rf'GRANT SELECT ON {re.escape(relation)}\b')
        self.assertIn(
            'GRANT USAGE ON SCHEMA collection, processing, reference, serving', SQL
        )

    def test_selected_query_columns_are_covered_by_the_column_grants(self):
        aliases = {
            'e': 'processing.events',
            's': 'collection.raw_snapshots',
            'rc': 'collection.raw_contents',
            'r': 'processing.event_reviews',
            'ie': 'serving.incident_evidence',
        }
        selected = {relation: set() for relation in REQUIRED_COLUMNS}
        for alias, column in re.findall(r'\b(e|s|rc|r|ie)\.([a-z_]+)\b', CURATION_SOURCE):
            selected[aliases[alias]].add(column)
        resolver_query = RESOLVER_SOURCE.split('REFERENCE_QUERY = """', 1)[1].split('"""', 1)[0]
        selected['reference.krl_stations'].update(
            re.findall(r'\b([a-z_]+)\b', resolver_query.split('FROM', 1)[0].replace('SELECT', ''))
        )
        for relation, columns in selected.items():
            self.assertTrue(columns <= REQUIRED_COLUMNS[relation], (relation, columns - REQUIRED_COLUMNS[relation]))

    def test_rls_policies_are_select_only_and_candidate_scoped(self):
        for policy, relation in (
            ('curation_readonly_events_select', 'processing.events'),
            ('curation_readonly_event_reviews_select', 'processing.event_reviews'),
            ('curation_readonly_snapshots_select', 'collection.raw_snapshots'),
            ('curation_readonly_raw_contents_select', 'collection.raw_contents'),
            ('curation_readonly_station_reference_select', 'reference.krl_stations'),
            ('curation_readonly_incident_evidence_select', 'serving.incident_evidence'),
        ):
            self.assertRegex(
                SQL,
                rf'CREATE POLICY {policy} ON {re.escape(relation)}\s+FOR SELECT TO moobby_incident_readonly',
            )
        self.assertIn("event_status = 'potential_disruption' AND event_index > 0", SQL)
        self.assertIn('ALTER TABLE serving.incident_evidence ENABLE ROW LEVEL SECURITY', SQL)

    def test_no_write_grants_or_privileged_function_grants(self):
        self.assertNotRegex(
            SQL,
            r'GRANT\s+(?:[^;]*,\s*)?(?:INSERT|UPDATE|DELETE|TRUNCATE|EXECUTE)\b',
        )
        self.assertNotIn('TO anon', SQL)
        self.assertNotIn('TO authenticated', SQL)
        self.assertIn('has_any_column_privilege', SQL)
        self.assertIn('has_function_privilege', SQL)
        self.assertIn('routine.prosecdef', SQL)

    def test_operator_verification_queries_cover_membership_reads_writes_and_functions(self):
        self.assertIn('Manual post-deployment verification', SQL)
        self.assertIn('inherited_role', SQL)
        self.assertIn('AS can_select', SQL)
        self.assertIn('AS can_insert', SQL)
        self.assertIn('AS can_update', SQL)
        self.assertIn('AS can_delete', SQL)
        self.assertIn('AS can_truncate', SQL)
        self.assertIn('callable_security_definer', SQL)

    def test_pooler_login_suffix_is_documented_for_matching_cli(self):
        cli = (ROOT / 'scripts/curate_incidents.py').read_text(encoding='utf-8')
        self.assertIn("username.startswith(CURATION_LOGIN + '.')", cli)
        self.assertIn('moobby_incident_readonly_login', SQL)


if __name__ == '__main__':
    unittest.main()
