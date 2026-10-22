import unittest
import json
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch

from scripts.incident_curation_writes import (
    CurationWriteError,
    approve_incident,
    attach_evidence,
    confirm_location,
    load_curator_connection,
    read_event_review,
    review_evidence,
    run_transaction,
)
from scripts.curate_incidents import main as curation_main


INCIDENT_ID = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())
EVIDENCE_ID = str(uuid.uuid4())


class FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, results=None, fail_constraints=False):
        self.results = list(results or [])
        self.fail_constraints = fail_constraints
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql == 'SELECT current_user':
            return FakeResult(('curator-1',))
        if sql == 'SET CONSTRAINTS ALL IMMEDIATE' and self.fail_constraints:
            raise RuntimeError('deferred constraint violation')
        return self.results.pop(0) if self.results else FakeResult()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def connect_to(connection):
    def connect(**_kwargs):
        return connection
    return connect


class IncidentCurationWriteTests(unittest.TestCase):
    WRITE_COMMANDS = {
        'create': [],
        'attach-evidence': [
            'incident-id', 'event-id', '--event-review-revision', '1', '--rationale', 'candidate link',
        ],
        'review-evidence': [
            'evidence-id', '--decision', 'rejected', '--reviewer-id', 'reviewer',
            '--rationale', 'not a match',
        ],
        'review': [
            'incident-id', '--status', 'pending', '--reviewer-id', 'reviewer',
            '--rationale', 'needs review',
        ],
        'confirm-location': [
            'incident-id', 'event-id', '--entity-index', '0', '--reviewer-id', 'reviewer',
            '--rationale', 'confirmed manually',
        ],
        'approve': ['incident-id', '--reviewer-id', 'reviewer', '--rationale', 'approved by reviewer'],
        'withdraw': ['incident-id', '--reviewer-id', 'reviewer', '--rationale', 'withdrawn by reviewer'],
        'verify': [
            'incident-id', '--status', 'inconclusive', '--verifier-id', 'reviewer',
            '--rationale', 'insufficient corroboration', '--source-name', 'Source',
        ],
    }

    def test_every_write_subcommand_accepts_apply_and_confirm(self):
        for command, arguments in self.WRITE_COMMANDS.items():
            with self.subTest(command=command):
                output = io.StringIO()
                with patch('scripts.incident_curation_writes.run_write_command', return_value='offline stub'):
                    with redirect_stdout(output):
                        result = curation_main([command, *arguments, '--apply', '--confirm'])
                self.assertEqual(result, 0)
                self.assertIn('offline stub', output.getvalue())

    def test_write_help_documents_confirmation_and_dry_run_default(self):
        for command in self.WRITE_COMMANDS:
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
                    curation_main([command, '--help'])
                self.assertEqual(exit_info.exception.code, 0)
                help_text = ' '.join(output.getvalue().split())
                self.assertIn('--apply', help_text)
                self.assertIn('--confirm', help_text)
                self.assertIn('rolled back', help_text)

    def test_read_only_list_and_inspect_help_remain_without_write_flags(self):
        for command in ('list', 'inspect'):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
                    curation_main([command, '--help'])
                self.assertEqual(exit_info.exception.code, 0)
                self.assertNotIn('--apply', output.getvalue())
                self.assertNotIn('--confirm', output.getvalue())

    def test_transaction_layer_rejects_apply_without_confirmation(self):
        with self.assertRaisesRegex(CurationWriteError, 'requires the explicit --confirm flag'):
            run_transaction(
                {'user': 'curator'}, lambda _connection: self.fail('must not run'),
                apply=True, confirmed=False,
            )

    def test_curator_login_requires_its_own_private_connection_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'curator.json'
            path.write_text(json.dumps({
                'host': 'db.example.supabase.co', 'dbname': 'postgres',
                'user': 'moobby_incident_curator_login', 'password': 'offline-only',
            }), encoding='utf-8')
            os.chmod(path, 0o600)
            loaded = load_curator_connection(path)
            self.assertEqual(loaded['user'], 'moobby_incident_curator_login')
            self.assertEqual(loaded['sslmode'], 'require')
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(CurationWriteError, 'permissions must be 0600'):
                load_curator_connection(path)

    def test_rejected_evidence_cannot_be_primary(self):
        evidence = (EVIDENCE_ID, INCIDENT_ID, EVENT_ID, 1, 'supports', 'proposed', False, 'initial')
        connection = FakeConnection(results=[
            FakeResult(evidence),
            FakeResult((INCIDENT_ID, None, None, 'draft')),
            FakeResult(evidence),
        ])
        with self.assertRaisesRegex(CurationWriteError, 'Primary evidence must be accepted'):
            review_evidence(
                connection, evidence_id=EVIDENCE_ID, decision='rejected',
                reviewer_id='curator-1', rationale='Not the same incident.',
                event_review_status='accepted', primary=True,
            )
        self.assertFalse(any(sql.startswith('UPDATE serving.incident_evidence')
                             for sql, _ in connection.statements))

    def test_pending_processing_review_cannot_be_accepted_as_evidence(self):
        evidence = (EVIDENCE_ID, INCIDENT_ID, EVENT_ID, 1, 'supports', 'proposed', False, 'initial')
        connection = FakeConnection(results=[
            FakeResult(evidence),
            FakeResult((INCIDENT_ID, 'Repair at Sudirman', 'other', 'draft')),
            FakeResult(evidence),
        ])
        with self.assertRaisesRegex(CurationWriteError, 'accepted or corrected event review'):
            review_evidence(
                connection, evidence_id=EVIDENCE_ID, decision='accepted',
                reviewer_id='curator-1', rationale='The pinned model result is not reviewed.',
                event_review_status='pending',
            )
        self.assertFalse(any(sql.startswith('UPDATE serving.incident_evidence')
                             for sql, _ in connection.statements))

    def test_active_event_association_blocks_a_new_review_revision(self):
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, None, None, 'draft')),
            FakeResult(('existing-evidence-id',)),
        ])
        with self.assertRaisesRegex(CurationWriteError, 'active event association'):
            attach_evidence(
                connection, incident_id=INCIDENT_ID, event_id=EVENT_ID,
                event_review_revision=2, event_status='potential_disruption',
                event_index=1, event_review_status='accepted', rationale='Same reported repair.',
            )
        self.assertFalse(any(sql.startswith('INSERT INTO serving.incident_evidence')
                             for sql, _ in connection.statements))

    def test_rejected_association_allows_a_new_pinned_review_revision(self):
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, 'draft title', 'other', 'draft')),
            FakeResult(None),
            FakeResult(('new-evidence-id',)),
        ])
        result = attach_evidence(
            connection, incident_id=INCIDENT_ID, event_id=EVENT_ID,
            event_review_revision=2, event_status='potential_disruption', event_index=1,
            event_review_status='accepted',
            rationale='New accepted human review provides the revised basis.',
        )
        self.assertEqual(result, {'evidence_id': 'new-evidence-id', 'link_status': 'proposed'})
        duplicate_check = connection.statements[1]
        self.assertIn("link_status IN ('proposed','accepted')", duplicate_check[0])
        self.assertIn('event_review_revision = %s', duplicate_check[0])
        self.assertEqual(duplicate_check[1], (INCIDENT_ID, EVENT_ID, 2))
        insert = next(params for sql, params in connection.statements
                      if sql.startswith('INSERT INTO serving.incident_evidence'))
        self.assertEqual(insert[2], 2)
        self.assertFalse(any(sql.startswith('UPDATE serving.incident_evidence')
                             for sql, _ in connection.statements))

    def test_same_rejected_review_revision_cannot_be_relinked(self):
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, 'draft title', 'other', 'draft')),
            FakeResult(('rejected-evidence-id',)),
        ])
        with self.assertRaisesRegex(CurationWriteError, 'review revision was already linked'):
            attach_evidence(
                connection, incident_id=INCIDENT_ID, event_id=EVENT_ID,
                event_review_revision=1, event_status='potential_disruption', event_index=1,
                event_review_status='accepted',
                rationale='Do not reuse the rejected pin.',
            )

    def test_rejected_processing_revision_cannot_be_proposed(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(CurationWriteError, 'rejected processing-event revision'):
            attach_evidence(
                connection, incident_id=INCIDENT_ID, event_id=EVENT_ID,
                event_review_revision=2, event_status='potential_disruption', event_index=1,
                event_review_status='rejected', rationale='Do not propose rejected review.',
            )
        self.assertEqual(connection.statements, [])

    def test_two_sudirman_events_are_only_proposed_not_auto_merged_or_accepted(self):
        escalator_events = (
            'e262c174-f13a-4c92-89cb-b478b795a3c0',
            'f8feb4a5-e561-4e76-943d-a62182be039a',
        )
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, 'Escalator repair at Sudirman', 'infrastructure_issue', 'draft')),
            FakeResult(None), FakeResult(('evidence-1',)),
            FakeResult((INCIDENT_ID, 'Escalator repair at Sudirman', 'infrastructure_issue', 'draft')),
            FakeResult(None), FakeResult(('evidence-2',)),
        ])
        results = [attach_evidence(
            connection, incident_id=INCIDENT_ID, event_id=event_id,
            event_review_revision=1, event_status='potential_disruption', event_index=index,
            event_review_status='pending',
            rationale='Proposed same-location escalator repair correlation; human review required.',
        ) for index, event_id in enumerate(escalator_events, start=1)]
        self.assertEqual([result['link_status'] for result in results], ['proposed', 'proposed'])
        inserts = [(sql, params) for sql, params in connection.statements
                   if sql.startswith('INSERT INTO serving.incident_evidence')]
        self.assertEqual(len(inserts), 2)
        self.assertTrue(all("'proposed'" in sql and params[3] == 'supports'
                            for sql, params in inserts))

    def test_missing_processing_review_revision_is_rejected(self):
        connection = FakeConnection(results=[FakeResult(None)])
        with self.assertRaisesRegex(CurationWriteError, 'revision does not exist'):
            read_event_review({'user': 'readonly'}, EVENT_ID, 99, connect=connect_to(connection))
        self.assertEqual(connection.statements[0][0], 'SET TRANSACTION READ ONLY')

    def test_location_requires_an_unambiguously_resolved_station(self):
        connection = FakeConnection(results=[FakeResult((INCIDENT_ID, None, None, 'draft'))])
        with self.assertRaisesRegex(CurationWriteError, 'unambiguously resolved'):
            confirm_location(
                connection, incident_id=INCIDENT_ID,
                station={'resolution_status': 'ambiguous', 'entity_type': 'station'},
                reviewer_id='curator-1', rationale='Could refer to two stations.',
            )
        self.assertFalse(any(sql.startswith('UPDATE serving.incidents SET location_name')
                             for sql, _ in connection.statements))

    def test_location_confirmation_records_review_and_station_coordinates(self):
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, None, None, 'draft')),
            FakeResult((1,)),
        ])
        result = confirm_location(
            connection, incident_id=INCIDENT_ID,
            station={
                'resolution_status': 'resolved', 'location_semantics': 'mentioned_station_only',
                'station_id': 'open-krl:THB', 'source_station_code': 'THB',
                'canonical_name': 'Tanah Abang', 'latitude': -6.17, 'longitude': 106.82,
            }, reviewer_id='curator-1', rationale='Explicitly confirmed from the post context.',
        )
        self.assertEqual(result['review_status'], 'pending')
        self.assertTrue(any(sql.startswith('INSERT INTO serving.incident_reviews')
                            for sql, _ in connection.statements))
        update = next(params for sql, params in connection.statements
                      if sql.startswith('UPDATE serving.incidents SET location_name'))
        self.assertEqual(update[:4], ('Tanah Abang', 'open-krl:THB', -6.17, 106.82))

    def test_approval_requires_title_and_category_before_publication(self):
        connection = FakeConnection(results=[FakeResult((INCIDENT_ID, None, None, 'draft'))])
        with self.assertRaisesRegex(CurationWriteError, 'title and valid category'):
            approve_incident(
                connection, incident_id=INCIDENT_ID, reviewer_id='curator-1',
                rationale='Ready for publication.',
            )
        self.assertFalse(any('publication_status = \'approved\'' in sql
                             for sql, _ in connection.statements))

    def test_deferred_publication_constraint_failure_rolls_back_transaction(self):
        connection = FakeConnection(fail_constraints=True)
        with self.assertRaisesRegex(CurationWriteError, 'transaction failed'):
            run_transaction(
                {'user': 'curator'}, lambda db: 'would publish',
                apply=True, confirmed=True, connect=connect_to(connection),
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_review_decision_requires_rationale(self):
        connection = FakeConnection(results=[FakeResult((INCIDENT_ID, 'Repair', 'other', 'draft'))])
        with self.assertRaisesRegex(CurationWriteError, 'Review rationale is required'):
            approve_incident(
                connection, incident_id=INCIDENT_ID, reviewer_id='curator-1', rationale='  ',
            )

    def test_reviewer_identity_must_match_authenticated_database_login(self):
        connection = FakeConnection(results=[FakeResult((INCIDENT_ID, 'Repair', 'other', 'draft'))])
        from scripts.incident_curation_writes import record_incident_review
        with self.assertRaisesRegex(CurationWriteError, 'match the authenticated PostgreSQL login'):
            record_incident_review(
                connection, incident_id=INCIDENT_ID, reviewer_id='someone-else',
                status='pending', rationale='Review requested.',
            )
        self.assertFalse(any(sql.startswith('INSERT INTO serving.incident_reviews')
                             for sql, _ in connection.statements))

    def test_verification_is_explicit_and_append_only(self):
        connection = FakeConnection(results=[
            FakeResult((INCIDENT_ID, 'Escalator repair', 'infrastructure_issue', 'draft')),
            FakeResult((1,)),
            FakeResult(),
        ])
        from scripts.incident_curation_writes import record_verification
        result = record_verification(
            connection, incident_id=INCIDENT_ID, verifier_id='curator-1',
            status='inconclusive', source_name='KAI service notice',
            source_url='https://example.com/notice',
            evidence_summary='The notice does not confirm whether this event is the same repair.',
            checked_at='2026-10-22T10:00:00+07:00',
        )
        self.assertEqual(result['verification_status'], 'inconclusive')
        insert = next((sql, params) for sql, params in connection.statements
                      if sql.startswith('INSERT INTO serving.incident_verifications'))
        self.assertEqual(insert[1][2], 'inconclusive')
        self.assertFalse(any(sql.startswith('UPDATE serving.incident_verifications')
                             for sql, _ in connection.statements))


if __name__ == '__main__':
    unittest.main()
