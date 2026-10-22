import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.review_processing_events import (
    ADVISORY_LOCK_SEED,
    ProcessingReviewError,
    append_event_review,
    build_parser,
    load_reviewer_connection,
    run_review,
    validate_corrections,
)


EVENT_ID = 'e262c174-f13a-4c92-89cb-b478b795a3c0'


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class ReviewStore:
    """Small thread-safe fake that models the database advisory lock and append."""
    def __init__(self):
        self.lock = threading.Lock()
        self.session_user = 'moobby_processing_reviewer_login'
        self.effective_user = 'moobby_processing_reviewer_login'
        self.revisions = [(1, 'pending', None, '', {}, '2026-01-01T00:00:00Z')]
        self.calls = []
        self.fail_constraints = False
        self.commits = 0
        self.rollbacks = 0

    def connection(self, **_kwargs):
        return ReviewConnection(self)


class ReviewConnection:
    def __init__(self, store):
        self.store = store
        self.held = False
        self.snapshot = None

    def execute(self, sql, params=None):
        self.store.calls.append((sql, params))
        if 'pg_advisory_xact_lock' in sql:
            self.store.lock.acquire()
            self.held = True
            self.snapshot = list(self.store.revisions)
            return Result((None,))
        if sql.startswith('SELECT id FROM processing.events'):
            return Result((EVENT_ID,))
        if sql.startswith('SELECT COALESCE(max(revision)'):
            return Result((max(row[0] for row in self.store.revisions) + 1,))
        if sql == 'SELECT session_user':
            return Result((self.store.session_user,))
        if sql.startswith('INSERT INTO processing.event_reviews'):
            revision, status, reviewer, rationale, corrections = params[1:]
            self.store.revisions.append((revision, status, reviewer, rationale,
                                         json.loads(corrections), '2026-10-22T00:00:00Z'))
            return Result((revision, '2026-10-22T00:00:00Z'))
        if sql == 'SET CONSTRAINTS ALL IMMEDIATE' and self.store.fail_constraints:
            raise RuntimeError('simulated deferred constraint failure')
        return Result()

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        self.store.rollbacks += 1
        if self.held and self.snapshot is not None:
            self.store.revisions[:] = self.snapshot

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.held:
            self.store.lock.release()
            self.held = False
        return False


class ProcessingEventReviewTests(unittest.TestCase):
    def test_migration_has_additive_attributed_append_only_review_contract(self):
        migration = (Path(__file__).parents[1] / 'db/012_processing_event_review_workflow.sql').read_text()
        self.assertIn('ADD COLUMN reviewer_id text', migration)
        self.assertIn('CREATE ROLE moobby_processing_reviewer NOLOGIN', migration)
        self.assertIn('CREATE ROLE moobby_processing_reviewer_login LOGIN INHERIT', migration)
        self.assertIn('pg_advisory_xact_lock(hashtextextended(NEW.event_id::text, 8171901))', migration)
        self.assertIn('NEW.revision <> expected_revision', migration)
        self.assertIn('CREATE TRIGGER processing_event_reviews_append_only', migration)
        self.assertIn('BEFORE TRUNCATE ON processing.event_reviews', migration)
        self.assertIn('REVOKE UPDATE, DELETE, TRUNCATE ON processing.event_reviews', migration)
        self.assertIn('CREATE UNIQUE INDEX incident_evidence_one_active_event_per_incident_idx', migration)
        self.assertIn("WHERE link_status IN ('proposed','accepted')", migration)
        self.assertIn("pg_has_role(current_user, 'moobby_processing_reviewer', 'member')", migration)
        self.assertIn('NEW.reviewer_id IS DISTINCT FROM session_user', migration)
        self.assertIn('revision > 1 AND reviewer_id = session_user', migration)
        self.assertNotIn('NEW.reviewer_id IS DISTINCT FROM current_user', migration)
        self.assertNotIn('revision > 1 AND reviewer_id = current_user', migration)
        self.assertNotIn('UPDATE PROCESSING.EVENT_REVIEWS', migration.upper())

    def test_orvix_importer_revision_one_remains_nullable_and_compatible(self):
        importer = (Path(__file__).parents[1] / 'scripts/import_orvix_results.py').read_text()
        migration = (Path(__file__).parents[1] / 'db/012_processing_event_review_workflow.sql').read_text()
        self.assertIn("(event_id,revision,review_status,review_notes)\n        VALUES (%s,1,'pending','')", importer)
        self.assertIn('IF NEW.revision = 1 THEN', migration)
        self.assertIn('NEW.reviewer_id IS NOT NULL', migration)

    def test_reviewer_connection_requires_private_dedicated_login_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reviewer.json'
            path.write_text(json.dumps({
                'host': 'db.example.supabase.co', 'dbname': 'postgres',
                'user': 'moobby_processing_reviewer_login.projectref', 'password': 'offline',
            }), encoding='utf-8')
            os.chmod(path, 0o600)
            self.assertEqual(load_reviewer_connection(path)['user'],
                             'moobby_processing_reviewer_login.projectref')
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(ProcessingReviewError, 'permissions must be 0600'):
                load_reviewer_connection(path)

    def test_corrections_are_limited_and_corrected_requires_values(self):
        with self.assertRaisesRegex(ProcessingReviewError, 'requires at least one correction'):
            validate_corrections('corrected', {})
        with self.assertRaisesRegex(ProcessingReviewError, 'Unsupported correction'):
            validate_corrections('corrected', {'event_status': 'no_disruption'})
        self.assertEqual(validate_corrections('corrected', {'severity': 'unknown'}),
                         {'severity': 'unknown'})

    def test_decision_appends_revision_and_preserves_history(self):
        store = ReviewStore()
        with store.connection() as connection:
            result = append_event_review(
                connection, EVENT_ID, 'accepted', 'Evidence supports this event.', {},
            )
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['review_status'], 'accepted')
        self.assertEqual([item[0] for item in store.revisions], [1, 2])
        self.assertEqual(store.revisions[0][1], 'pending')
        statements = [sql.upper() for sql, _ in store.calls]
        self.assertFalse(any(sql.startswith('UPDATE PROCESSING.EVENT_REVIEWS') or
                             sql.startswith('DELETE FROM PROCESSING.EVENT_REVIEWS')
                             for sql in statements))
        self.assertIn(('SELECT session_user', None), store.calls)

    def test_session_identity_is_used_when_effective_role_is_switched(self):
        store = ReviewStore()
        store.session_user = 'moobby_processing_reviewer_login'
        store.effective_user = 'moobby_processing_reviewer'
        with store.connection() as connection:
            result = append_event_review(
                connection, EVENT_ID, 'accepted', 'Reviewed under SET ROLE.', {},
            )
        self.assertEqual(result['reviewer_id'], store.session_user)
        self.assertNotEqual(result['reviewer_id'], store.effective_user)
        insert = next(params for sql, params in store.calls
                      if sql.startswith('INSERT INTO processing.event_reviews'))
        self.assertEqual(insert[3], 'moobby_processing_reviewer_login')

    def test_database_contract_rejects_group_identity_spoofing(self):
        migration = (Path(__file__).parents[1] / 'db/012_processing_event_review_workflow.sql').read_text()
        self.assertIn('IF NEW.reviewer_id IS DISTINCT FROM session_user THEN', migration)
        self.assertIn('revision > 1 AND reviewer_id = session_user', migration)
        self.assertIn("pg_has_role(current_user, 'moobby_processing_reviewer', 'member')", migration)

    def test_rollback_integration_script_covers_role_switch_and_leaves_transaction_rolled_back(self):
        script = (Path(__file__).parents[1] /
                  'db/tests/012_processing_review_rollback_test.sql').read_text()
        self.assertIn('SET LOCAL ROLE moobby_processing_reviewer', script)
        self.assertIn('reviewer identity spoof was unexpectedly accepted', script)
        self.assertIn('duplicate active evidence association was unexpectedly accepted', script)
        self.assertIn('SET CONSTRAINTS ALL IMMEDIATE;', script)
        self.assertIn('ROLLBACK;', script)
        self.assertNotIn('COMMIT;', script)

    def test_rejected_decision_is_append_only_and_has_rationale(self):
        store = ReviewStore()
        with store.connection() as connection:
            result = append_event_review(
                connection, EVENT_ID, 'rejected', 'Not a disruption report.', {},
            )
        self.assertEqual(result['review_status'], 'rejected')
        self.assertEqual(store.revisions[-1][3], 'Not a disruption report.')

    def test_two_concurrent_reviewers_allocate_distinct_sequential_revisions(self):
        store = ReviewStore()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def review(decision):
            try:
                barrier.wait()
                with store.connection() as connection:
                    results.append(append_event_review(
                        connection, EVENT_ID, decision, 'Concurrent human review.', {},
                    )['revision'])
            except Exception as error:  # surfaced below with context
                errors.append(error)

        threads = [threading.Thread(target=review, args=(decision,))
                   for decision in ('accepted', 'rejected')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(errors)
        self.assertEqual(sorted(results), [2, 3])
        self.assertEqual(sorted(row[0] for row in store.revisions), [1, 2, 3])
        self.assertTrue(all(params == (EVENT_ID, ADVISORY_LOCK_SEED)
                            for sql, params in store.calls if 'pg_advisory_xact_lock' in sql))

    def test_dry_run_rolls_back_and_confirmation_is_mandatory_for_apply(self):
        store = ReviewStore()
        with self.assertRaisesRegex(ProcessingReviewError, 'requires both --apply and --confirm'):
            run_review({}, EVENT_ID, 'accepted', 'Rationale', {}, apply=True,
                       confirmed=False, connect=store.connection)
        result = run_review({}, EVENT_ID, 'accepted', 'Rationale', {},
                            connect=store.connection)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(store.commits, 0)
        self.assertEqual(store.rollbacks, 1)
        self.assertEqual([row[0] for row in store.revisions], [1])

    def test_deferred_error_rolls_back_entire_review_transaction(self):
        store = ReviewStore()
        store.fail_constraints = True
        with self.assertRaisesRegex(ProcessingReviewError, 'RuntimeError'):
            run_review({}, EVENT_ID, 'accepted', 'Rationale', {}, apply=True,
                       confirmed=True, connect=store.connection)
        self.assertEqual(store.commits, 0)
        self.assertEqual(store.rollbacks, 1)
        self.assertEqual([row[0] for row in store.revisions], [1])

    def test_cli_exposes_review_decisions_and_confirmation_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            'review', EVENT_ID, '--decision', 'corrected', '--rationale', 'Correction',
            '--corrections-json', '{"severity":"unknown"}', '--apply', '--confirm',
        ])
        self.assertEqual(args.decision, 'corrected')
        self.assertTrue(args.apply and args.confirm)


if __name__ == '__main__':
    unittest.main()
