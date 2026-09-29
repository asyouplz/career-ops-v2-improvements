"""Performance contracts with synthetic data; no production state or credentials."""
import copy
import datetime as dt
import json
import unittest
from unittest import mock

import server
from test_server import StoreTests, HTTPTests, candidate, row


class PerformanceTests(unittest.TestCase):
    setUp = StoreTests.setUp
    export = StoreTests.export

    def move(self, status='excluded'):
        job = self.store.job_context('li_1')[0]
        return self.store.move({'id': job['id'], 'status': status, 'expected_updated_at': job['decision_revision']})

    def test_move_and_undo_build_only_before_and_after_views(self):
        job = self.store.job_context('li_1')[0]
        with mock.patch.object(self.store, 'snapshot', wraps=self.store.snapshot) as snapshot:
            result = self.store.move({'id': 'li_1', 'status': 'excluded', 'expected_updated_at': job['decision_revision']})
            self.assertEqual(snapshot.call_count, 2)
        self.assertNotIn(server.MOVEMENT_WARNING, result['dashboard']['warnings'])
        with mock.patch.object(self.store, 'snapshot', wraps=self.store.snapshot) as snapshot:
            self.store.undo_move({'token': result['undo_token']})
            self.assertEqual(snapshot.call_count, 2)

    def test_unchanged_move_uses_one_view_and_does_not_write(self):
        self.move()
        job = self.store.job_context('li_1')[0]
        with mock.patch.object(self.store, 'snapshot', wraps=self.store.snapshot) as snapshot, mock.patch.object(server, 'atomic_json', wraps=server.atomic_json) as writes:
            result = self.store.move({'id': 'li_1', 'status': 'excluded', 'expected_updated_at': job['decision_revision']})
            self.assertTrue(result['unchanged'])
            self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(writes.call_count, 0)

    def test_note_returns_saved_revision_without_third_view(self):
        job = self.store.job_context('li_1')[0]
        with mock.patch.object(self.store, 'snapshot', wraps=self.store.snapshot) as snapshot:
            result = self.store.update_note('li_1', {'note': 'Synthetic note', 'expected_updated_at': job['decision_revision']})
            self.assertEqual(snapshot.call_count, 2)
        self.assertNotEqual(result['job']['decision_revision'], job['decision_revision'])
        self.assertEqual(result['job']['note'], 'Synthetic note')

    def test_response_calculation_does_not_consume_undo_window(self):
        clock = [1000.0]
        original = self.store.snapshot
        def slow_snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 3.0
            return result
        with mock.patch.object(self.store, 'snapshot', side_effect=slow_snapshot), mock.patch.object(server.time, 'time', side_effect=lambda: clock[0]):
            result = self.move()
            expiry = dt.datetime.fromisoformat(result['undo_expires_at'].replace('Z', '+00:00')).timestamp()
            self.assertEqual(expiry - clock[0], 10)

    def test_tracker_conversion_is_once_per_row_and_refreshes_next_view(self):
        self.store.rows = [row(n) for n in range(1, 21)]
        self.export([candidate(n) for n in range(1, 21)])
        with mock.patch.object(self.store, '_tracker_job', wraps=self.store._tracker_job) as convert:
            self.store.dashboard()
            self.assertEqual(convert.call_count, 20)
        self.store.rows[0]['status'] = 'Applied'
        self.assertEqual(self.store.dashboard()['applications'][0]['canonical_status'], 'Applied')

    def test_alias_cache_does_not_share_mutable_results_or_ignore_changes(self):
        job = candidate(1)
        before = server.aliases(job)
        before.clear()
        self.assertIn('li_1', server.aliases(job))
        job['url'] = 'https://www.wanted.co.kr/wd/1000'
        self.assertIn(job['url'], server.aliases(job))
        self.assertNotIn('https://www.wanted.co.kr/wd/1', server.aliases(job))

    def test_index_preserves_ambiguous_matches_and_posting_conflicts(self):
        rows = [row(1), row(2)]
        rows[1].update(company=rows[0]['company'], role=rows[0]['role'])
        job = candidate(1)
        with self.store.lookup_scope():
            self.assertIsNone(self.store.matched_row(job, rows))
        rows[0]['notes'] = 'https://www.wanted.co.kr/wd/100'
        rows[1]['notes'] = 'https://www.wanted.co.kr/wd/1'
        with self.store.lookup_scope():
            self.assertEqual(self.store.matched_row(job, rows)['num'], 2)

    def test_index_keeps_first_export_match_and_latest_decision_tie_order(self):
        job = candidate(1)
        first = dict(job, dashboard_eligible=False)
        second = dict(job, dashboard_eligible=True)
        decisions = {'first': {'job': job, 'updated_at': 'same'}, 'second': {'job': job, 'updated_at': 'same'}}
        with self.store.lookup_scope():
            self.assertFalse(self.store.can_restore_recommendation(job, exported=[first, second]))
            self.assertEqual(self.store.matching_decision(job, decisions)[0], 'first')

    def test_failed_post_save_view_reports_partial_commit(self):
        job = self.store.job_context('li_1')[0]
        original = self.store.snapshot
        calls = [0]
        def fail_after(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise server.APIError(503, 'test_read_failure', 'synthetic failure')
            return original(*args, **kwargs)
        with mock.patch.object(self.store, 'snapshot', side_effect=fail_after), self.assertRaises(server.APIError) as error:
            self.store.move({'id': 'li_1', 'status': 'excluded', 'expected_updated_at': job['decision_revision']})
        self.assertTrue(error.exception.details['partial_commit'])
        self.assertEqual(self.store.decision_data()['decisions']['li_1']['status'], 'excluded')


class PerformanceHTTPTests(unittest.TestCase):
    setUp = HTTPTests.setUp
    export = HTTPTests.export
    shutdown = HTTPTests.shutdown
    request = HTTPTests.request
    auth = HTTPTests.auth

    def test_static_cache_and_private_response_boundaries(self):
        folder = self.c.static / '_next/static/chunks'
        folder.mkdir(parents=True)
        (folder / 'page-abc12345.js').write_text('/* synthetic */')
        (folder / 'page.js').write_text('/* unversioned */')
        self.assertEqual(self.request('GET', '/_next/static/chunks/page-abc12345.js')[1]['Cache-Control'], 'public, max-age=31536000, immutable')
        self.assertEqual(self.request('GET', '/_next/static/chunks/page.js')[1]['Cache-Control'], 'no-store')
        self.assertEqual(self.request('GET', '/')[1]['Cache-Control'], 'no-cache')
        headers = self.request('GET', '/api/dashboard', headers=self.auth())[1]
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn('snapshot;dur=', headers['Server-Timing'])

    def test_request_metrics_omit_tokens_query_values_and_job_identifiers(self):
        auth = self.auth()
        with mock.patch.object(server.logging, 'info') as log:
            self.request('GET', '/api/jobs/li_1/history?private=synthetic-secret', headers=auth)
        metrics = [json.loads(call.args[1]) for call in log.call_args_list if call.args[0] == 'career_request %s']
        self.assertEqual(metrics[0]['route'], '/api/jobs/:id/history')
        encoded = json.dumps(metrics)
        for private in ['li_1', 'synthetic-secret', auth['X-CSRF-Token'], auth['Cookie']]:
            self.assertNotIn(private, encoded)


if __name__ == '__main__':
    unittest.main()
