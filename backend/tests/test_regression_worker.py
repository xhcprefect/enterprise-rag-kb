"""Independent regression worker lifecycle with real PG jobs and deterministic models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import patch

from tests._env import TEST_URL
from tests import test_feedback_regression_executor as helpers

if TEST_URL:
    from app.core.config import settings
    from app.services import feedback_regression_jobs as jobs, regression_worker as worker, worker_status
    SessionLocal = helpers.SessionLocal


class RegressionWorkerTests(helpers._ExecutionFixture):
    def run_until_completed(self):
        stop = threading.Event()
        completed = threading.Event()
        actual_complete = worker._complete
        def complete(claim, outcome):
            try:
                result = actual_complete(claim, outcome)
                completed.set()
                return result
            finally:
                stop.set()
        watchdog = threading.Timer(10, stop.set)
        watchdog.start()
        try:
            with patch.object(worker, '_complete', side_effect=complete), \
                 patch.object(settings, 'FEEDBACK_REGRESSION_JOB_POLL_SECONDS', 0.05):
                self.assertEqual(worker.run(stop), 0)
            self.assertTrue(completed.is_set(), 'worker did not publish within 10 seconds')
        finally:
            watchdog.cancel()

    def test_worker_run_consumes_explicit_job_and_publishes_new_result(self):
        feedback_id, target = self.feedback()
        before = self.original_rows(target.conversation_id)
        job = self.fx.create_job_ok(feedback_id, revision=1)
        self.run_until_completed()
        actual = self.fx.get_job(job['job_id']).json()['data']
        self.assertEqual(actual['status'], 'succeeded')
        self.assertTrue(actual['result']['passed'])
        self.assertEqual(actual['attempt_count'], 1)
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(before, self.original_rows(target.conversation_id))

    def test_worker_retry_keeps_all_usage_and_does_not_log_provider_body(self):
        feedback_id, _ = self.feedback()
        job = self.fx.create_job_ok(feedback_id, revision=1)
        self.chat.mode = 'retry'
        with self.assertLogs('app.services.regression_worker', level='INFO') as captured:
            self.run_until_completed()
        self.assertNotIn('PRIVATE_PROVIDER_BODY', '\n'.join(captured.output))
        actual = self.fx.get_job(job['job_id']).json()['data']
        self.assertEqual(actual['status'], 'succeeded')
        self.assertEqual(actual['attempt_count'], 2)
        self.assertEqual(len(self.chat.calls), 2)
        self.assertEqual(actual['usage']['attempts'], 4)
        self.assertEqual(actual['usage']['failed_attempts'], 1)
        self.assertEqual(actual['usage']['input_tokens'], 54)
        self.assertEqual(actual['usage']['output_tokens'], 8)
        self.assertIsNone(actual['usage']['total_cost'])

    def test_cooperative_stop_releases_valid_lease_and_retains_usage(self):
        feedback_id, _ = self.feedback()
        job = self.fx.create_job_ok(feedback_id, revision=1)
        stop = threading.Event()
        self.chat.mode = 'stall'
        results = []
        thread = threading.Thread(target=lambda: results.append(worker.run(stop)))
        thread.start()
        try:
            self.assertTrue(self.chat.entered.wait(4))
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        finally:
            stop.set()
            self.chat.unblock.set()
            thread.join(5)
        self.assertEqual(results, [0])
        actual = self.fx.get_job(job['job_id']).json()['data']
        self.assertEqual(actual['status'], 'queued')
        self.assertEqual(actual['attempt_count'], 1)
        self.assertIsNone(self.fx.body_row(job['job_id']))
        self.assertEqual(actual['usage']['cancelled_attempts'], 1)
        self.assertTrue(self.chat.closed.is_set())

    def test_expired_attempt_recovery_executes_with_new_lease(self):
        feedback_id, _ = self.feedback()
        job, old_claim, _loaded = self.loaded(feedback_id)
        with SessionLocal() as db:
            row = db.get(helpers.fixtures.FeedbackRegressionJob, old_claim.job_id)
            original_deadline = row.deadline_at
            row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        self.run_until_completed()
        actual = self.fx.get_job(job['job_id']).json()['data']
        self.assertEqual(actual['status'], 'succeeded')
        self.assertEqual(actual['attempt_count'], 2)
        with SessionLocal() as db:
            row = db.get(helpers.fixtures.FeedbackRegressionJob, old_claim.job_id)
            self.assertEqual(row.deadline_at, original_deadline)
            check = jobs.check_lease(db, job_id=old_claim.job_id, lease_id=old_claim.lease_id)
            self.assertFalse(check.valid)

    def test_real_regression_queue_counts_and_safe_latest_failure(self):
        first, _ = self.feedback()
        second, _ = self.feedback()
        _running, claim, _loaded = self.loaded(second)
        self.fx.create_job_ok(first, revision=1)
        with SessionLocal() as db:
            db.get(helpers.fixtures.FeedbackRegressionJob, claim.job_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        stats = worker_status._collect_worker_stats(2)
        role = stats['roles']['regression']
        self.assertEqual((role['pending'], role['processing'], role['stale_tasks']), (1, 1, 1))
        self.assertIsNotNone(role['oldest_pending_age_seconds'])
        # Mark the queued job failed via a real claim/fail transition, retaining only safe codes.
        with SessionLocal() as db:
            row = db.get(helpers.fixtures.FeedbackRegressionJob, claim.job_id)
            row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
            db.commit()
            jobs.fail(db, job_id=claim.job_id, lease_id=claim.lease_id,
                      error_code='PRIVATE_PROVIDER_BODY_MUST_NOT_BE_LOGGED')
        stats = worker_status._collect_worker_stats(2)
        self.assertEqual(stats['roles']['regression']['last_failure']['code'], 'execution_failed')
        payload = worker_status.build_worker_status(stats)
        self.assertEqual([item['role'] for item in payload['roles']], ['ingest', 'summary', 'project', 'regression'])
        unavailable = worker_status.build_worker_status({'status': 'unavailable'})
        self.assertTrue(unavailable['degraded'])
        role = unavailable['roles'][-1]
        self.assertEqual(role['state'], 'unknown')
        self.assertIsNone(role['pending'])

    def test_idle_worker_does_not_enqueue_or_call_models(self):
        stop = threading.Event()
        # After a real empty claim, stop; the worker must not invent any jobs/model work.
        real_claim = worker._claim_one
        def claim():
            result = real_claim()
            stop.set()
            return result
        with patch.object(worker, '_claim_one', side_effect=claim):
            self.assertEqual(worker.run(stop), 0)
        self.assertEqual(self.chat.calls, [])
        self.assertEqual(self.fx.counts()['jobs'], 0)


if __name__ == '__main__':
    unittest.main()
