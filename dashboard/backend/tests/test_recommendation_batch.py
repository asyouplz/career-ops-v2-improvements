"""Regression checks use synthetic temporary data only, never production records.

Run with run_checks.py so all fixtures use the same backend module.
"""
import copy
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import server
from test_server import FakeStore, candidate, row
import test_server as fixtures


class RecommendationBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.c = server.Config()
        self.c.data = Path(temporary.name)
        self.c.root = self.c.data
        self.c.export = self.c.data / 'dashboard-candidates.json'
        self.c.decisions = self.c.data / 'dashboard-decisions.json'
        self.c.tracker = self.c.data / 'applications.md'
        self.c.gmail_credentials = self.c.data / 'missing-credentials.json'
        self.batch_file = self.c.data / 'dashboard-recommendations.json'
        self.store = FakeStore(self.c)
        self.run_at = server.now()
        self.pool = [candidate(i) for i in range(1, 9)]
        self.export()

    def export(self, *, jobs=None, source_run_at=None, **extra):
        payload = {'version': 1, 'source_run_at': source_run_at or self.run_at,
                   'generated_at': server.now(), 'jobs': copy.deepcopy(self.pool if jobs is None else jobs),
                   'sources': [{'source_id': 'wanted', 'source_label': '원티드'}]}
        payload.update(extra)
        server.atomic_json(self.c.export, payload)

    def dashboard(self, store=None):
        return (store or self.store).dashboard()

    def jobs(self, dashboard=None):
        return [job for group in (dashboard or self.dashboard())['sites'] for job in group['jobs']]

    def ids(self, dashboard=None):
        return [job['id'] for job in self.jobs(dashboard)]

    def selected(self, ident, dashboard=None):
        return next(job for job in self.jobs(dashboard) if job['id'] == ident)

    def move(self, ident='li_1', status='excluded', store=None, **extra):
        store = store or self.store
        job = store.job_context(ident)[0]
        return store.move({'id': ident, 'status': status,
                           'expected_updated_at': job['decision_revision'], **extra})

    def test_first_read_persists_top_five_without_changing_decisions(self):
        self.assertEqual(self.ids(), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertTrue(self.batch_file.is_file())
        self.assertEqual(self.batch_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.c.decisions.exists())
        saved = self.batch_file.read_bytes()
        self.dashboard()
        self.assertEqual(self.batch_file.read_bytes(), saved)

    def test_exclude_retains_reviewed_row_without_automatic_replacement(self):
        result = self.move()
        dashboard = result['dashboard']
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        reviewed = self.selected('li_1', dashboard)
        self.assertEqual(reviewed['decision_status'], 'excluded')
        self.assertTrue(reviewed['recommendation_reviewed'])
        self.assertEqual(reviewed['movement_status'], 'excluded')
        self.assertEqual(dashboard['stats']['recommended'], 4)
        self.assertEqual(dashboard['excluded']['total'], 1)
        self.assertEqual(dashboard['pending']['total'], 0)
        self.assertNotIn('li_6', self.ids(dashboard))

    def test_pending_and_excluded_archives_stay_separate(self):
        self.move('li_1', 'excluded')
        self.move('li_2', 'pending')
        dashboard = self.dashboard()
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(dashboard['stats']['recommended'], 3)
        self.assertEqual([j['id'] for j in self.store.archive('excluded', 0, 5)['items']], ['li_1'])
        self.assertEqual([j['id'] for j in self.store.archive('pending', 0, 5)['items']], ['li_2'])
        self.assertTrue(self.selected('li_2', dashboard)['recommendation_reviewed'])

    def test_restart_reuses_same_reviewed_batch(self):
        self.move()
        saved = self.batch_file.read_bytes()
        restarted = FakeStore(self.c)
        dashboard = self.dashboard(restarted)
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(self.selected('li_1', dashboard)['decision_status'], 'excluded')
        self.assertEqual(self.batch_file.read_bytes(), saved)

    def test_generated_at_and_pool_rewrite_do_not_reset_same_run(self):
        self.move()
        self.export(jobs=[candidate(99), *reversed(self.pool)], generated_at=server.now())
        dashboard = self.dashboard()
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(dashboard['stats']['recommended'], 4)
        self.assertNotIn('li_99', self.ids(dashboard))

    def test_next_successful_source_run_refills_and_preserves_exclusion(self):
        self.move()
        before = self.c.decisions.read_bytes()
        next_run = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
        self.export(source_run_at=next_run)
        dashboard = self.dashboard()
        self.assertEqual(self.ids(dashboard), ['li_2', 'li_3', 'li_4', 'li_5', 'li_6'])
        self.assertEqual(dashboard['stats']['recommended'], 5)
        self.assertEqual(dashboard['excluded']['total'], 1)
        self.assertEqual(self.c.decisions.read_bytes(), before)

    def test_manual_refill_is_idempotent_and_does_not_undo_decisions(self):
        self.move('li_1', 'excluded')
        self.move('li_2', 'pending')
        before = self.c.decisions.read_bytes()
        result = self.store.refill_recommendations()
        self.assertTrue(result['ok'])
        self.assertEqual(self.ids(result['dashboard']), ['li_3', 'li_4', 'li_5', 'li_6', 'li_7'])
        self.assertEqual(result['dashboard']['stats']['recommended'], 5)
        self.assertEqual(self.c.decisions.read_bytes(), before)
        again = self.store.refill_recommendations()
        self.assertEqual(self.ids(again['dashboard']), self.ids(result['dashboard']))
        self.assertEqual(self.c.decisions.read_bytes(), before)

    def test_undo_returns_same_row_to_original_slot_and_restores_reason(self):
        before_ids = self.ids()
        result = self.move(note='Synthetic exclusion reason')
        self.assertEqual(self.selected('li_1')['note'], 'Synthetic exclusion reason')
        result = self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(self.ids(result['dashboard']), before_ids)
        restored = self.selected('li_1', result['dashboard'])
        self.assertEqual(restored['decision_status'], 'new')
        self.assertFalse(restored.get('recommendation_reviewed', False))
        self.assertFalse(restored.get('note'))
        self.assertEqual(result['dashboard']['stats']['recommended'], 5)
        self.assertEqual(result['dashboard']['excluded']['total'], 0)

    def test_failed_move_revision_does_not_change_saved_selection(self):
        self.dashboard()
        before = self.batch_file.read_bytes()
        with self.assertRaises(server.APIError) as caught:
            self.store.move({'id': 'li_1', 'status': 'excluded', 'expected_updated_at': 'stale'})
        self.assertEqual(caught.exception.code, 'decision_conflict')
        self.assertEqual(self.batch_file.read_bytes(), before)
        self.assertFalse(self.c.decisions.exists())

    def test_other_store_update_blocks_undo_and_keeps_latest_state(self):
        result = self.move()
        other = FakeStore(self.c)
        self.move('li_1', 'pending', store=other)
        with self.assertRaises(server.APIError) as caught:
            self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(caught.exception.code, 'undo_conflict')
        self.assertEqual(self.selected('li_1')['decision_status'], 'pending')
        self.assertEqual(self.dashboard()['excluded']['total'], 0)

    def test_closed_selected_row_is_retained_but_disabled_until_refill(self):
        self.dashboard()
        self.pool[0].update(liveness='expired', dashboard_eligible=False)
        self.export()
        dashboard = self.dashboard()
        unavailable = self.selected('li_1', dashboard)
        self.assertTrue(unavailable['recommendation_unavailable'])
        self.assertFalse(unavailable['movable'])
        self.assertEqual(unavailable['allowed_moves'], [])
        self.assertEqual(dashboard['stats']['recommended'], 4)
        self.assertNotIn('li_6', self.ids(dashboard))
        result = self.store.refill_recommendations()
        self.assertEqual(self.ids(result['dashboard']), ['li_2', 'li_3', 'li_4', 'li_5', 'li_6'])

    def test_expired_liveness_evidence_does_not_backfill_on_read(self):
        self.dashboard()
        self.pool[0]['verified_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=73)).isoformat()
        self.export()
        dashboard = self.dashboard()
        self.assertTrue(self.selected('li_1', dashboard)['recommendation_unavailable'])
        self.assertFalse(self.selected('li_1', dashboard)['movable'])
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])

    def test_missing_selected_row_does_not_pick_a_different_offer_implicitly(self):
        self.dashboard()
        self.export(jobs=self.pool[1:])
        dashboard = self.dashboard()
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertTrue(self.selected('li_1', dashboard)['recommendation_unavailable'])
        self.assertFalse(self.selected('li_1', dashboard)['movable'])

    def test_unique_top_five_are_selected_per_site(self):
        self.export(jobs=[candidate(1), candidate(1), *[candidate(i) for i in range(2, 8)],
                          *[candidate(i, 'saramin') for i in range(20, 27)]])
        dashboard = self.dashboard()
        groups = {g['id']: g for g in dashboard['sites']}
        self.assertEqual([j['id'] for j in groups['wanted']['jobs']], [f'li_{i}' for i in range(1, 6)])
        self.assertEqual([j['id'] for j in groups['saramin']['jobs']], [f'li_{i}' for i in range(20, 25)])
        self.assertEqual(len(self.ids(dashboard)), len(set(self.ids(dashboard))))

    def test_existing_application_is_never_selected_or_restorable(self):
        self.store.rows = [row(1, 'Applied')]
        dashboard = self.dashboard()
        self.assertNotIn('li_1', self.ids(dashboard))
        self.assertEqual(len(dashboard['applications']), 1)
        application = dashboard['applications'][0]
        self.assertFalse(application['movable'])
        with self.assertRaises(server.APIError) as caught:
            self.move(application['id'], 'excluded')
        self.assertEqual(caught.exception.code, 'application_exists')
        self.assertEqual(self.store.rows[0]['status'], 'Applied')

    def test_application_record_after_selection_cannot_be_moved_as_recommendation(self):
        self.dashboard()
        self.store.rows = [row(1, 'Applied')]
        dashboard = self.dashboard()
        self.assertEqual(dashboard['stats']['recommended'], 4)
        self.assertEqual(len(dashboard['applications']), 1)
        self.assertNotIn('li_6', self.ids(dashboard))
        for job in self.jobs(dashboard):
            if job['id'] == 'li_1':
                self.assertFalse(job['movable'])
                self.assertTrue(job['ever_applied'])
        with self.assertRaises(server.APIError):
            self.move('li_1', 'excluded')
        self.assertEqual(self.store.rows[0]['status'], 'Applied')

    def test_two_store_instances_keep_independent_moves_and_one_batch(self):
        self.dashboard()
        stores = [FakeStore(self.c), FakeStore(self.c)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.move, 'li_1', 'excluded', store=stores[0]),
                       pool.submit(self.move, 'li_2', 'pending', store=stores[1])]
            for future in futures:
                future.result(timeout=10)
        dashboard = self.dashboard(FakeStore(self.c))
        self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(self.selected('li_1', dashboard)['decision_status'], 'excluded')
        self.assertEqual(self.selected('li_2', dashboard)['decision_status'], 'pending')
        self.assertEqual(dashboard['stats']['recommended'], 3)
        self.assertTrue(json.loads(self.batch_file.read_text()))

    def test_parallel_first_reads_create_one_complete_batch(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(FakeStore(self.c).dashboard) for _ in range(4)]
            dashboards = [future.result(timeout=10) for future in futures]
        for dashboard in dashboards:
            self.assertEqual(self.ids(dashboard), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertTrue(isinstance(json.loads(self.batch_file.read_text()), dict))

    def test_corrupt_batch_fails_closed_without_overwriting_file(self):
        self.batch_file.write_text('{broken')
        before = self.batch_file.read_bytes()
        with self.assertRaises(server.APIError):
            self.dashboard()
        self.assertEqual(self.batch_file.read_bytes(), before)


class RefillHTTPTests(unittest.TestCase):
    export = fixtures.StoreTests.export
    shutdown = fixtures.HTTPTests.shutdown
    request = fixtures.HTTPTests.request
    auth = fixtures.HTTPTests.auth

    def setUp(self):
        fixtures.HTTPTests.setUp(self)
        self.c.gmail_credentials = self.c.data / 'missing-credentials.json'

    def test_refill_requires_login_origin_and_csrf_before_mutation(self):
        self.store.dashboard()
        self.store.decide({'id': 'li_1', 'status': 'excluded'})
        batch = self.c.data / 'dashboard-recommendations.json'
        before = batch.read_bytes()
        self.assertEqual(self.request('POST', '/api/recommendations/refill', {})[0], 401)
        auth = self.auth()
        self.assertEqual(self.request('POST', '/api/recommendations/refill', {},
                                      {**auth, 'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/recommendations/refill', {},
                                      {k: v for k, v in auth.items() if k != 'X-CSRF-Token'})[0], 403)
        self.assertEqual(batch.read_bytes(), before)
        status, _, result = self.request('POST', '/api/recommendations/refill', {}, auth)
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual([j['id'] for s in result['dashboard']['sites'] for j in s['jobs']],
                         ['li_2', 'li_3', 'li_4', 'li_5', 'li_6'])
        self.assertEqual(result['dashboard']['excluded']['total'], 1)

    def test_generic_dashboard_refresh_does_not_refill(self):
        self.store.decide({'id': 'li_1', 'status': 'excluded'})
        status, _, result = self.request('GET', '/api/dashboard', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual([j['id'] for s in result['sites'] for j in s['jobs']],
                         ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(result['stats']['recommended'], 4)
        self.assertEqual(result['sites'][0]['jobs'][0]['decision_status'], 'excluded')


if __name__ == '__main__':
    unittest.main(verbosity=2)
